#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clonador_reels.py — Automação de edição em lote para Instagram Reels / TikTok (9:16)
Pipeline 100% FFmpeg. Sem MoviePy.
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table


# ─── CONFIGURAÇÃO CENTRAL ─────────────────────────────────────────────────────
CONFIG = {
    # Canvas de saída (formato 9:16 para Instagram/TikTok)
    "canvas_width":  1080,
    "canvas_height": 1920,

    # Vídeo sobreposto no fundo
    "video_width":  800,       # largura do vídeo (px). Altura calculada proporcionalmente.
    "position_x":  "center",  # posição horizontal: "center" ou valor inteiro em px
    "position_y":  0.40,      # fração vertical do canvas (0.40 = 40% do topo)
    # Safe zone Instagram: UI cobre os últimos ~250px inferiores
    # Safe zone TikTok:    UI cobre os últimos ~300px inferiores

    # Qualidade de exportação
    "output_fps":    30,
    "output_crf":    18,       # 18=alta qualidade, 23=médio, 28=comprimido
    "output_preset": "slow",   # ultrafast/fast/medium/slow/veryslow
    "audio_bitrate": "192k",

    # Fallback automático: se o encode falhar, tenta de novo com estes valores
    "fallback_enabled": True,
    "fallback_crf":     23,
    "fallback_preset":  "medium",

    # Paralelismo — quantos vídeos processar ao mesmo tempo.
    # None = automático (metade dos cores, mínimo 1, máximo 4).
    # Use 1 para processar um de cada vez (comportamento antigo).
    "workers": None,

    # Técnicas anti-ban — valores aleatórios por vídeo dentro dos intervalos
    "trim_start":        0.1,
    "speed_range":       (1.02, 1.05),
    "brightness_range":  (0.01, 0.02),
    "saturation_range":  (-0.02, 0.02),
    "zoom_range":        (1.01, 1.03),
    "flip_chance":       0.0,
    # ATENÇÃO: flip_chance > 0 ESPELHA o vídeo — imagem e textos ficam VIRADOS.

    # ─── Máscara de watermark / indicadores do app ───────────────────────────
    # Cobre regiões do vídeo FONTE (ex: "1x"/"3x" de velocidade, logo do TikTok).
    # Coordenadas em px, relativas ao vídeo ORIGINAL (antes do redimensionamento).
    # Deixe a lista vazia para desativar.
    # Exemplo: {"x": 20, "y": 40, "w": 90, "h": 50, "mode": "blur"}
    #   mode: "blur" (borra) ou "box" (tampa com cor sólida)
    "watermark_masks": [],
    "mask_box_color":  "black",

    # Comportamento do script
    "skip_existing": True,
    "min_duration":  3.0,
}

# ─── PATHS ────────────────────────────────────────────────────────────────────
DIR_BASE    = Path(__file__).parent
DIR_ENTRADA = DIR_BASE / "entrada"
DIR_SAIDA   = DIR_BASE / "saida"
DIR_LOGS    = DIR_SAIDA / "logs"
FUNDO_PATH  = DIR_BASE / "fundo.png"
LOG_FILE    = DIR_LOGS / "processamento.log"
STATE_FILE  = DIR_SAIDA / "processados.json"
CSV_FILE    = DIR_SAIDA / "relatorio.csv"

console = Console()

# Locks para acesso concorrente ao estado e ao log
_state_lock = threading.Lock()


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    """Configura log simultâneo: terminal colorido (rich) + arquivo em disco."""
    DIR_LOGS.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("clonador")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    rich_handler = RichHandler(console=console, show_path=False, markup=True)
    rich_handler.setLevel(logging.INFO)
    logger.addHandler(rich_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    return logger


def resolver_workers() -> int:
    """Define quantos vídeos processar em paralelo."""
    cfg = CONFIG.get("workers")
    if isinstance(cfg, int) and cfg > 0:
        return cfg
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2))


# ─── VALIDAÇÃO ────────────────────────────────────────────────────────────────

def validar_ambiente(logger: logging.Logger) -> list[Path]:
    """
    Valida FFmpeg, fundo.png e pastas.
    Retorna lista de .mp4 em /entrada ordenada alfabeticamente.
    """
    for cmd in ["ffmpeg", "ffprobe"]:
        try:
            subprocess.run([cmd, "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(f"{cmd} não encontrado no PATH.")
            logger.error("Windows: https://ffmpeg.org/download.html → extrair e adicionar /bin ao PATH")
            logger.error("Mac:     brew install ffmpeg")
            logger.error("Linux:   sudo apt install ffmpeg")
            sys.exit(1)

    if not FUNDO_PATH.exists():
        logger.error(f"fundo.png ausente: {FUNDO_PATH}")
        logger.error(
            f"Adicione um arquivo fundo.png de "
            f"{CONFIG['canvas_width']}x{CONFIG['canvas_height']}px na raiz do script."
        )
        sys.exit(1)

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(FUNDO_PATH)],
        capture_output=True,
        text=True,
    )
    info = json.loads(probe.stdout)
    video_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        logger.error("fundo.png não é uma imagem válida (nenhum stream detectado).")
        sys.exit(1)
    w = video_streams[0]["width"]
    h = video_streams[0]["height"]
    if w != CONFIG["canvas_width"] or h != CONFIG["canvas_height"]:
        logger.error(
            f"fundo.png deve ser {CONFIG['canvas_width']}x{CONFIG['canvas_height']}px. "
            f"Encontrado: {w}x{h}px."
        )
        sys.exit(1)

    DIR_ENTRADA.mkdir(exist_ok=True)
    DIR_SAIDA.mkdir(exist_ok=True)

    videos = sorted(
        [p for p in DIR_ENTRADA.iterdir() if p.suffix.lower() == ".mp4"],
        key=lambda p: p.name.lower(),
    )
    if not videos:
        logger.warning("Nenhum .mp4 encontrado em /entrada. Encerrando.")
        sys.exit(0)

    logger.info(f"Encontrados [bold]{len(videos)}[/bold] vídeos para processar.")
    return videos


# ─── FFPROBE ──────────────────────────────────────────────────────────────────

def _probe(path: Path) -> dict:
    """Executa ffprobe e retorna o JSON de format + streams."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def obter_info(path: Path) -> dict:
    """
    Retorna {'duracao': float, 'audio': bool, 'width': int, 'height': int}.
    Uma única chamada ao ffprobe em vez de várias.
    """
    data = _probe(path)

    duration = data.get("format", {}).get("duration")
    video_stream = None
    tem_audio = False

    for stream in data.get("streams", []):
        tipo = stream.get("codec_type")
        if tipo == "video" and video_stream is None:
            video_stream = stream
            if not duration and "duration" in stream:
                duration = stream["duration"]
        elif tipo == "audio":
            tem_audio = True

    if not duration:
        raise ValueError(f"Não foi possível determinar duração de {path.name}")

    return {
        "duracao": float(duration),
        "audio":   tem_audio,
        "width":   int(video_stream.get("width", 0)) if video_stream else 0,
        "height":  int(video_stream.get("height", 0)) if video_stream else 0,
    }


# ─── ANTI-BAN ─────────────────────────────────────────────────────────────────

def gerar_params_antiban(seed: int) -> dict:
    """Gera parâmetros de variação. Mesma seed = mesmos parâmetros (reproducível)."""
    rng = random.Random(seed)
    return {
        "seed":       seed,
        "speed":      round(rng.uniform(*CONFIG["speed_range"]), 4),
        "brightness": round(rng.uniform(*CONFIG["brightness_range"]), 4),
        "saturation": round(rng.uniform(*CONFIG["saturation_range"]), 4),
        "zoom":       round(rng.uniform(*CONFIG["zoom_range"]), 4),
        "flip":       rng.random() < CONFIG["flip_chance"],
        "trim_start": CONFIG["trim_start"],
    }


# ─── MÁSCARA DE WATERMARK ─────────────────────────────────────────────────────

def construir_filtros_mascara() -> list[str]:
    """
    Monta os filtros que cobrem watermarks/indicadores do vídeo fonte.
    Aplicado ANTES do redimensionamento, em coordenadas do vídeo original.
    """
    filtros = []
    for m in CONFIG.get("watermark_masks", []):
        x, y = int(m["x"]), int(m["y"])
        w, h = int(m["w"]), int(m["h"])
        modo = m.get("mode", "blur")

        if modo == "box":
            cor = m.get("color", CONFIG["mask_box_color"])
            filtros.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={cor}:t=fill")
        else:
            # delogo interpola a região a partir das bordas — menos visível que uma caixa
            filtros.append(f"delogo=x={x}:y={y}:w={w}:h={h}")
    return filtros


# ─── CONSTRUÇÃO DOS FILTROS ───────────────────────────────────────────────────

def construir_filtros(params: dict, com_audio: bool) -> tuple[str, str | None]:
    """
    Monta filter_complex e audio_filter para FFmpeg.
    Ordem: máscara → speed → zoom/crop → eq → flip → overlay no fundo.
    """
    canvas_w   = CONFIG["canvas_width"]
    canvas_h   = CONFIG["canvas_height"]
    video_w    = CONFIG["video_width"]
    speed      = params["speed"]
    brightness = params["brightness"]
    saturation = 1.0 + params["saturation"]
    zoom       = params["zoom"]
    flip       = params["flip"]

    # Largura com zoom — múltiplo de 2 (exigência do libx264)
    zoomed_w = int(video_w * zoom)
    if zoomed_w % 2 != 0:
        zoomed_w += 1

    video_filters = []

    # 1. Máscaras primeiro, em coordenadas do vídeo original
    video_filters.extend(construir_filtros_mascara())

    # 2. Transformações
    video_filters += [
        f"setpts=PTS/{speed}",
        f"scale={zoomed_w}:-2",
        f"crop={video_w}:ih",
        f"eq=brightness={brightness}:saturation={saturation:.4f}",
    ]
    if flip:
        video_filters.append("hflip")  # ESPELHA o conteúdo

    pos_x_cfg = CONFIG["position_x"]
    if pos_x_cfg == "center":
        overlay_x = f"({canvas_w}-overlay_w)/2"
    else:
        overlay_x = str(int(pos_x_cfg))

    overlay_y = int(canvas_h * CONFIG["position_y"])

    filter_complex = (
        f"[0:v]scale={canvas_w}:{canvas_h}[bg];"
        f"[1:v]{','.join(video_filters)}[vid];"
        f"[bg][vid]overlay={overlay_x}:{overlay_y}:shortest=1[out]"
    )

    # atempo sincroniza o áudio com a aceleração (range válido: 0.5–2.0)
    audio_filter = f"atempo={speed}" if com_audio else None

    return filter_complex, audio_filter


# ─── RENDERIZAÇÃO ─────────────────────────────────────────────────────────────

def _montar_comando(
    input_path: Path,
    output_path: Path,
    params: dict,
    com_audio: bool,
    crf: int,
    preset: str,
) -> list[str]:
    """Monta a linha de comando completa do FFmpeg."""
    filter_complex, audio_filter = construir_filtros(params, com_audio)

    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-loop", "1",
        "-i", str(FUNDO_PATH),          # input 0: fundo estático
        "-ss", str(params["trim_start"]),
        "-i", str(input_path),          # input 1: vídeo fonte
        "-filter_complex", filter_complex,
    ]

    if audio_filter:
        cmd += ["-filter:a", audio_filter]

    cmd += ["-map", "[out]"]

    if com_audio:
        cmd += ["-map", "1:a"]

    cmd += [
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-r", str(CONFIG["output_fps"]),
    ]

    if com_audio:
        cmd += ["-c:a", "aac", "-b:a", CONFIG["audio_bitrate"]]

    cmd += [
        "-shortest",                 # termina quando o vídeo fonte acabar (o fundo é infinito)
        "-map_metadata", "-1",
        "-fflags", "+bitexact",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd


def renderizar_video(
    input_path: Path,
    output_path: Path,
    params: dict,
    com_audio: bool,
    logger: logging.Logger,
) -> tuple[bool, str]:
    """
    Executa FFmpeg. Em caso de falha, tenta uma vez com preset/CRF mais leves.
    Retorna (sucesso, qualidade_usada).
    """
    tentativas = [(CONFIG["output_crf"], CONFIG["output_preset"])]
    if CONFIG["fallback_enabled"]:
        tentativas.append((CONFIG["fallback_crf"], CONFIG["fallback_preset"]))

    ultimo_erro = ""

    for idx, (crf, preset) in enumerate(tentativas):
        cmd = _montar_comando(input_path, output_path, params, com_audio, crf, preset)

        if idx > 0:
            logger.warning(
                f"[yellow]RETRY[/yellow] {input_path.name} — "
                f"tentando com crf={crf} preset={preset}"
            )

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as exc:
            ultimo_erro = str(exc)
            continue

        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True, f"crf{crf}/{preset}"

        stderr = proc.stderr or ""
        linhas = [l for l in stderr.splitlines() if l.strip()]
        ultimo_erro = linhas[-1] if linhas else f"código {proc.returncode}"

        if output_path.exists():
            output_path.unlink()

    logger.error(f"FFmpeg falhou em {input_path.name}: {ultimo_erro}")
    return False, ""


# ─── ESTADO PERSISTIDO ────────────────────────────────────────────────────────

def carregar_estado() -> dict:
    """Carrega histórico de processamento do JSON persistido entre sessões."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def salvar_estado(estado: dict):
    """Persiste histórico em JSON. Thread-safe."""
    with _state_lock:
        STATE_FILE.write_text(
            json.dumps(estado, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def exportar_csv(estado: dict, logger: logging.Logger):
    """Gera relatorio.csv com o mapeamento entrada → saída e os parâmetros usados."""
    colunas = [
        "arquivo_origem", "arquivo_saida", "status", "tempo_s", "tamanho_mb",
        "qualidade", "speed", "zoom", "brightness", "saturation", "flip",
        "seed", "timestamp",
    ]
    try:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=colunas)
            writer.writeheader()
            for nome, dados in sorted(estado.items()):
                p = dados.get("params", {})
                writer.writerow({
                    "arquivo_origem": nome,
                    "arquivo_saida":  dados.get("output", ""),
                    "status":         dados.get("status", ""),
                    "tempo_s":        dados.get("tempo_s", ""),
                    "tamanho_mb":     dados.get("tamanho_mb", ""),
                    "qualidade":      dados.get("qualidade", ""),
                    "speed":          p.get("speed", ""),
                    "zoom":           p.get("zoom", ""),
                    "brightness":     p.get("brightness", ""),
                    "saturation":     p.get("saturation", ""),
                    "flip":           p.get("flip", ""),
                    "seed":           p.get("seed", ""),
                    "timestamp":      dados.get("timestamp", ""),
                })
        logger.info(f"Relatório CSV salvo em {CSV_FILE.name}")
    except OSError as exc:
        logger.warning(f"Não foi possível gravar o CSV: {exc}")


# ─── ORQUESTRADOR ─────────────────────────────────────────────────────────────

def processar_video(
    video_path: Path,
    index: int,
    total: int,
    estado: dict,
    logger: logging.Logger,
    dry_run: bool = False,
) -> str:
    """
    Orquestra o processamento de um vídeo.
    Retorna: "sucesso" | "pulado" | "erro"
    """
    nome = video_path.name
    output_path = DIR_SAIDA / f"{video_path.stem}_edited.mp4"
    prefixo = f"[{index}/{total}] {nome}"

    # Skip por estado persistido
    if CONFIG["skip_existing"] and estado.get(nome, {}).get("status") == "sucesso":
        logger.info(f"[yellow]PULADO[/yellow] {prefixo} — já processado")
        return "pulado"

    # Skip por arquivo de saída existente
    if CONFIG["skip_existing"] and output_path.exists() and nome not in estado:
        logger.info(f"[yellow]PULADO[/yellow] {prefixo} — saída já existe")
        return "pulado"

    # Informações do vídeo (duração, áudio, resolução) numa só chamada
    try:
        info = obter_info(video_path)
    except ValueError as exc:
        logger.error(f"[red]ERRO[/red] {prefixo} — {exc}")
        return "erro"

    duracao = info["duracao"]
    duracao_util = duracao - CONFIG["trim_start"]
    if duracao_util < CONFIG["min_duration"]:
        logger.warning(
            f"[yellow]PULADO[/yellow] {prefixo} — duração útil {duracao_util:.1f}s "
            f"< mínimo {CONFIG['min_duration']}s"
        )
        return "pulado"

    # Seed estável via MD5 (hash() do Python é randomizado entre execuções)
    seed = int(hashlib.md5(nome.encode("utf-8")).hexdigest()[:8], 16)
    params = gerar_params_antiban(seed)

    duracao_final = duracao_util / params["speed"]
    audio_str = "com áudio" if info["audio"] else "MUDO"

    logger.info(
        f"[cyan]{prefixo}[/cyan] — {info['width']}x{info['height']} | "
        f"{duracao:.1f}s → {duracao_final:.1f}s | {audio_str}"
    )
    logger.debug(
        f"{nome} anti-ban: seed={seed} speed={params['speed']}x zoom={params['zoom']}x "
        f"flip={params['flip']} brightness={params['brightness']:+.4f} "
        f"saturation={params['saturation']:+.4f}"
    )

    if params["flip"]:
        logger.warning(
            f"[yellow]AVISO[/yellow] {nome} — flip ativo: o conteúdo será ESPELHADO. "
            f"Defina flip_chance=0.0 no CONFIG para desativar."
        )

    if dry_run:
        return "pulado"

    t_inicio = time.time()
    sucesso, qualidade = renderizar_video(
        video_path, output_path, params, info["audio"], logger
    )
    tempo = round(time.time() - t_inicio, 1)

    if sucesso:
        tamanho_mb = round(output_path.stat().st_size / (1024 * 1024), 2)
        logger.info(
            f"[green]OK[/green] {prefixo} → {output_path.name} "
            f"({tempo}s, {tamanho_mb}MB, {qualidade})"
        )
        registro = {
            "status":     "sucesso",
            "output":     output_path.name,
            "tempo_s":    tempo,
            "tamanho_mb": tamanho_mb,
            "qualidade":  qualidade,
            "params":     params,
            "timestamp":  datetime.now().isoformat(),
        }
    else:
        logger.error(f"[red]ERRO[/red] {prefixo} — falhou após {tempo}s")
        registro = {
            "status":    "erro",
            "timestamp": datetime.now().isoformat(),
            "params":    params,
        }
        if output_path.exists():
            output_path.unlink()

    with _state_lock:
        estado[nome] = registro
    salvar_estado(estado)

    return "sucesso" if sucesso else "erro"


# ─── MAIN ──────────────────────────────────────────────────