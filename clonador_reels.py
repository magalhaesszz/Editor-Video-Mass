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
    "video_width":  800,
    "position_x":  "center",
    "position_y":  0.40,

    # Qualidade de exportação
    "output_fps":    30,
    "output_crf":    18,
    "output_preset": "slow",
    "audio_bitrate": "192k",

    # Fallback: se falhar, tenta com valores mais leves
    "fallback_enabled": True,
    "fallback_crf":     23,
    "fallback_preset":  "medium",

    # Paralelismo (None = automático: metade dos cores, máx 4)
    "workers": None,

    # ─── CORREÇÃO DE ESPELHAMENTO ─────────────────────────────────────────────
    # True  = corrige vídeos que chegam espelhados (câmera frontal, TikTok etc.)
    # False = não aplica nenhuma correção de espelhamento
    "fix_mirror": False,

    # ─── Anti-ban ─────────────────────────────────────────────────────────────
    "trim_start":       0.1,
    "speed_range":      (1.02, 1.05),
    "brightness_range": (0.01, 0.02),
    "saturation_range": (-0.02, 0.02),
    "zoom_range":       (1.01, 1.03),

    # ─── Máscara de watermark ─────────────────────────────────────────────────
    # Remove indicadores como "1.8x" gravados no vídeo.
    # x: "center" ou px. y: fração 0-1 ou px. mode: "blur" ou "box"
    # Deixe [] para desativar.
    "watermark_masks": [
        {"x": "center", "y": 0.08, "w": 130, "h": 80, "mode": "blur"},
    ],
    "mask_box_color": "black",

    # Comportamento
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
_state_lock = threading.Lock()


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    DIR_LOGS.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("clonador")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    rh = RichHandler(console=console, show_path=False, markup=True)
    rh.setLevel(logging.INFO)
    logger.addHandler(rh)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger


def resolver_workers() -> int:
    cfg = CONFIG.get("workers")
    if isinstance(cfg, int) and cfg > 0:
        return cfg
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2))


# ─── VALIDAÇÃO ────────────────────────────────────────────────────────────────

def validar_ambiente(logger: logging.Logger) -> list[Path]:
    for cmd in ["ffmpeg", "ffprobe"]:
        try:
            subprocess.run([cmd, "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(f"{cmd} não encontrado. Instale o FFmpeg e adicione ao PATH.")
            sys.exit(1)

    if not FUNDO_PATH.exists():
        logger.error(f"fundo.png não encontrado em {FUNDO_PATH}")
        sys.exit(1)

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(FUNDO_PATH)],
        capture_output=True, text=True,
    )
    info = json.loads(probe.stdout)
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        logger.error("fundo.png inválido.")
        sys.exit(1)
    w, h = vs[0]["width"], vs[0]["height"]
    if w != CONFIG["canvas_width"] or h != CONFIG["canvas_height"]:
        logger.error(f"fundo.png deve ser {CONFIG['canvas_width']}x{CONFIG['canvas_height']}px. Encontrado: {w}x{h}px.")
        sys.exit(1)

    DIR_ENTRADA.mkdir(exist_ok=True)
    DIR_SAIDA.mkdir(exist_ok=True)

    videos = sorted(
        [p for p in DIR_ENTRADA.iterdir() if p.suffix.lower() == ".mp4"],
        key=lambda p: p.name.lower(),
    )
    if not videos:
        logger.warning("Nenhum .mp4 em /entrada.")
        sys.exit(0)

    logger.info(f"Encontrados [bold]{len(videos)}[/bold] vídeos.")
    return videos


# ─── FFPROBE ──────────────────────────────────────────────────────────────────

def obter_info(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError(f"ffprobe falhou em {path.name}")

    duration = data.get("format", {}).get("duration")
    video_stream = None
    tem_audio = False

    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and video_stream is None:
            video_stream = s
            if not duration and "duration" in s:
                duration = s["duration"]
        elif s.get("codec_type") == "audio":
            tem_audio = True

    if not duration:
        raise ValueError(f"Duração não encontrada em {path.name}")

    return {
        "duracao": float(duration),
        "audio":   tem_audio,
        "width":   int(video_stream.get("width", 0)) if video_stream else 0,
        "height":  int(video_stream.get("height", 0)) if video_stream else 0,
    }


# ─── ANTI-BAN ─────────────────────────────────────────────────────────────────

def gerar_params_antiban(seed: int) -> dict:
    rng = random.Random(seed)
    return {
        "seed":       seed,
        "speed":      round(rng.uniform(*CONFIG["speed_range"]), 4),
        "brightness": round(rng.uniform(*CONFIG["brightness_range"]), 4),
        "saturation": round(rng.uniform(*CONFIG["saturation_range"]), 4),
        "zoom":       round(rng.uniform(*CONFIG["zoom_range"]), 4),
        "trim_start": CONFIG["trim_start"],
    }


# ─── CONSTRUÇÃO DOS FILTROS ───────────────────────────────────────────────────

def construir_filter_complex(params: dict, src_w: int, src_h: int) -> str:
    canvas_w   = CONFIG["canvas_width"]
    canvas_h   = CONFIG["canvas_height"]
    video_w    = CONFIG["video_width"]
    speed      = params["speed"]
    brightness = params["brightness"]
    saturation = 1.0 + params["saturation"]
    zoom       = params["zoom"]

    zoomed_w = int(video_w * zoom)
    if zoomed_w % 2 != 0:
        zoomed_w += 1

    video_filters = []

    # ── 1. Corrige espelhamento do vídeo de entrada ──────────────────────────
    if CONFIG.get("fix_mirror"):
        video_filters.append("hflip")

    # ── 2. Remove watermarks/indicadores do app ──────────────────────────────
    for m in CONFIG.get("watermark_masks", []):
        w_m, h_m = int(m["w"]), int(m["h"])
        x_cfg = m.get("x", 0)
        y_cfg = m.get("y", 0)
        x = max(0, (src_w - w_m) // 2) if x_cfg == "center" else int(x_cfg)
        y = int(src_h * y_cfg) if isinstance(y_cfg, float) and 0 <= y_cfg <= 1 else int(y_cfg)
        if m.get("mode", "blur") == "box":
            cor = m.get("color", CONFIG["mask_box_color"])
            video_filters.append(f"drawbox=x={x}:y={y}:w={w_m}:h={h_m}:color={cor}:t=fill")
        else:
            video_filters.append(f"delogo=x={x}:y={y}:w={w_m}:h={h_m}")

    # ── 3. Transformações anti-ban ────────────────────────────────────────────
    video_filters += [
        f"setpts=PTS/{speed}",
        f"scale={zoomed_w}:-2",
        f"crop={video_w}:ih",
        f"eq=brightness={brightness}:saturation={saturation:.4f}",
    ]

    # Posição do overlay
    if CONFIG["position_x"] == "center":
        overlay_x = f"({canvas_w}-overlay_w)/2"
    else:
        overlay_x = str(int(CONFIG["position_x"]))
    overlay_y = int(canvas_h * CONFIG["position_y"])

    return (
        f"[0:v]scale={canvas_w}:{canvas_h}[bg];"
        f"[1:v]{','.join(video_filters)}[vid];"
        f"[bg][vid]overlay={overlay_x}:{overlay_y}:shortest=1[out]"
    )


# ─── RENDERIZAÇÃO ─────────────────────────────────────────────────────────────

def renderizar_video(
    input_path: Path,
    output_path: Path,
    params: dict,
    com_audio: bool,
    src_w: int,
    src_h: int,
    logger: logging.Logger,
) -> tuple[bool, str]:
    tentativas = [(CONFIG["output_crf"], CONFIG["output_preset"])]
    if CONFIG["fallback_enabled"]:
        tentativas.append((CONFIG["fallback_crf"], CONFIG["fallback_preset"]))

    filter_complex = construir_filter_complex(params, src_w, src_h)
    audio_filter   = f"atempo={params['speed']}" if com_audio else None
    ultimo_erro    = ""

    for idx, (crf, preset) in enumerate(tentativas):
        if idx > 0:
            logger.warning(f"RETRY {input_path.name} — crf={crf} preset={preset}")

        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "error",
            "-loop", "1",
            "-i", str(FUNDO_PATH),
            "-ss", str(params["trim_start"]),
            "-i", str(input_path),
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
            "-shortest",
            "-map_metadata", "-1",
            "-fflags", "+bitexact",
            "-movflags", "+faststart",
            str(output_path),
        ]

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


# ─── ESTADO ───────────────────────────────────────────────────────────────────

def carregar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def salvar_estado(estado: dict):
    with _state_lock:
        STATE_FILE.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def exportar_csv(estado: dict, logger: logging.Logger):
    colunas = ["arquivo_origem", "arquivo_saida", "status", "tempo_s",
               "tamanho_mb", "qualidade", "speed", "zoom",
               "brightness", "saturation", "seed", "timestamp"]
    try:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=colunas)
            w.writeheader()
            for nome, d in sorted(estado.items()):
                p = d.get("params", {})
                w.writerow({
                    "arquivo_origem": nome,
                    "arquivo_saida":  d.get("output", ""),
                    "status":         d.get("status", ""),
                    "tempo_s":        d.get("tempo_s", ""),
                    "tamanho_mb":     d.get("tamanho_mb", ""),
                    "qualidade":      d.get("qualidade", ""),
                    "speed":          p.get("speed", ""),
                    "zoom":           p.get("zoom", ""),
                    "brightness":     p.get("brightness", ""),
                    "saturation":     p.get("saturation", ""),
                    "seed":           p.get("seed", ""),
                    "timestamp":      d.get("timestamp", ""),
                })
        logger.info(f"CSV salvo em {CSV_FILE.name}")
    except OSError as exc:
        logger.warning(f"Não foi possível gravar CSV: {exc}")


# ─── ORQUESTRADOR ─────────────────────────────────────────────────────────────

def processar_video(
    video_path: Path,
    index: int,
    total: int,
    estado: dict,
    logger: logging.Logger,
    dry_run: bool = False,
) -> str:
    nome = video_path.name
    output_path = DIR_SAIDA / f"{video_path.stem}_edited.mp4"
    prefixo = f"[{index}/{total}] {nome}"

    if CONFIG["skip_existing"] and estado.get(nome, {}).get("status") == "sucesso":
        logger.info(f"[yellow]PULADO[/yellow] {prefixo} — já processado")
        return "pulado"

    if CONFIG["skip_existing"] and output_path.exists() and nome not in estado:
        logger.info(f"[yellow]PULADO[/yellow] {prefixo} — saída já existe")
        return "pulado"

    try:
        info = obter_info(video_path)
    except ValueError as exc:
        logger.error(f"[red]ERRO[/red] {prefixo} — {exc}")
        return "erro"

    duracao_util = info["duracao"] - CONFIG["trim_start"]
    if duracao_util < CONFIG["min_duration"]:
        logger.warning(f"[yellow]PULADO[/yellow] {prefixo} — duração {duracao_util:.1f}s < mínimo {CONFIG['min_duration']}s")
        return "pulado"

    seed   = int(hashlib.md5(nome.encode("utf-8")).hexdigest()[:8], 16)
    params = gerar_params_antiban(seed)

    mirror_str = "corrigindo espelhamento" if CONFIG.get("fix_mirror") else "sem correção de espelho"
    logger.info(
        f"[cyan]{prefixo}[/cyan] — {info['width']}x{info['height']} | "
        f"{info['duracao']:.1f}s → {duracao_util/params['speed']:.1f}s | "
        f"{'áudio' if info['audio'] else 'mudo'} | {mirror_str}"
    )

    if dry_run:
        return "pulado"

    t = time.time()
    sucesso, qualidade = renderizar_video(
        video_path, output_path, params,
        info["audio"], info["width"], info["height"], logger,
    )
    tempo = round(time.time() - t, 1)

    if sucesso:
        mb = round(output_path.stat().st_size / (1024 * 1024), 2)
        logger.info(f"[green]OK[/green] {prefixo} → {output_path.name} ({tempo}s, {mb}MB, {qualidade})")
        registro = {
            "status": "sucesso", "output": output_path.name,
            "tempo_s": tempo, "tamanho_mb": mb,
            "qualidade": qualidade, "params": params,
            "timestamp": datetime.now().isoformat(),
        }
    else:
        logger.error(f"[red]ERRO[/red] {prefixo} — falhou após {tempo}s")
        registro = {"status": "erro", "timestamp": datetime.now().isoformat(), "params": params}
        if output_path.exists():
            output_path.unlink()

    with _state_lock:
        estado[nome] = registro
    salvar_estado(estado)
    return "sucesso" if sucesso else "erro"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Clonador de Reels — edição em lote 9:16")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem renderizar")
    parser.add_argument("--reprocess", metavar="ARQUIVO", help="Força reprocessar um arquivo")
    parser.add_argument("--workers", type=int, metavar="N", help="Vídeos em paralelo")
    args = parser.parse_args()

    logger = setup_logging()
    console.rule("[bold magenta]Clonador de Reels[/bold magenta]")
    logger.info(f"Sessão iniciada — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if CONFIG.get("fix_mirror"):
        logger.info("[cyan]fix_mirror=True[/cyan] — espelhamento dos vídeos de entrada será corrigido")

    if args.dry_run:
        logger.info("[yellow]DRY-RUN — nenhum vídeo será renderizado[/yellow]")

    videos = validar_ambiente(logger)
    estado = carregar_estado()

    if args.reprocess:
        alvo = Path(args.reprocess).name
        videos = [v for v in videos if v.name == alvo]
        if not videos:
            logger.error(f"'{alvo}' não encontrado em /entrada.")
            sys.exit(1)
        estado.pop(alvo, None)
        salvar_estado(estado)

    if args.workers and args.workers > 0:
        CONFIG["workers"] = args.workers
    workers = 1 if args.dry_run else resolver_workers()

    if CONFIG.get("watermark_masks"):
        logger.info(f"Máscara ativa: {len(CONFIG['watermark_masks'])} região(ões)")

    total = len(videos)
    contadores = {"sucesso": 0, "pulado": 0, "erro": 0}
    t_sessao = time.time()

    if workers > 1:
        logger.info(f"Paralelo com [bold]{workers}[/bold] workers.")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futuros = {
                pool.submit(processar_video, v, i, total, estado, logger, args.dry_run): v
                for i, v in enumerate(videos, 1)
            }
            for fut in as_completed(futuros):
                try:
                    contadores[fut.result()] += 1
                except Exception as exc:
                    logger.exception(f"Erro inesperado: {exc}")
                    contadores["erro"] += 1
    else:
        for i, v in enumerate(videos, 1):
            try:
                contadores[processar_video(v, i, total, estado, logger, args.dry_run)] += 1
            except Exception as exc:
                logger.exception(f"Erro inesperado: {exc}")
                contadores["erro"] += 1

    tempo_total = round(time.time() - t_sessao, 1)
    if not args.dry_run:
        exportar_csv(estado, logger)

    console.rule("[bold]Concluído[/bold]")
    tabela = Table(show_header=False, box=None, padding=(0, 2))
    tabela.add_row("[green]Sucesso[/green]",   f"[green]{contadores['sucesso']}[/green]")
    tabela.add_row("[yellow]Pulados[/yellow]",  f"[yellow]{contadores['pulado']}[/yellow]")
    tabela.add_row("[red]Erros[/red]",          f"[red]{contadores['erro']}[/red]")
    tabela.add_row("Total",                     str(total))
    tabela.add_row("Tempo",                     f"{tempo_total}s")
    if contadores["sucesso"]:
        tabela.add_row("Média/vídeo", f"{round(tempo_total/contadores['sucesso'],1)}s")
    console.print(tabela)
    sys.exit(1 if contadores["erro"] else 0)


if __name__ == "__main__":
    main()
