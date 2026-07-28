#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clonador_reels.py — Automação de edição em lote para Instagram Reels / TikTok (9:16)
Pipeline 100% FFmpeg. Sem MoviePy.
"""

import argparse
import json
import logging
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from tqdm import tqdm


# ─── CONFIGURAÇÃO CENTRAL ─────────────────────────────────────────────────────
# Ajuste todos os parâmetros aqui. Nenhum valor hardcoded no código abaixo.
CONFIG = {
    # Canvas de saída (formato 9:16 para Instagram/TikTok)
    "canvas_width":  1080,
    "canvas_height": 1920,

    # Vídeo sobreposto no fundo
    "video_width":  800,       # largura do vídeo (px). Altura calculada proporcionalmente.
    "position_x":  "center",  # posição horizontal: "center" ou valor inteiro em px
    "position_y":  0.40,      # fração vertical do canvas (0.40 = 40% do topo)
    # Safe zone Instagram: UI cobre os últimos ~250px inferiores (curtir/comentar)
    # Safe zone TikTok:    UI cobre os últimos ~300px inferiores (descrição + botões)

    # Qualidade de exportação
    "output_fps":    30,       # FPS fixo de saída (converte se fonte for diferente)
    "output_crf":    18,       # CRF libx264: 18=alta qualidade, 23=médio, 28=comprimido
    "output_preset": "slow",   # Preset libx264: ultrafast/fast/medium/slow/veryslow
    "audio_bitrate": "192k",   # Bitrate AAC

    # Técnicas anti-ban — valores aleatórios por vídeo dentro dos intervalos abaixo
    "trim_start":        0.1,           # segundos removidos do início (elimina frame inicial duplicado)
    "speed_range":       (1.02, 1.05),  # fator de aceleração — muda hash do arquivo
    "brightness_range":  (0.01, 0.02),  # variação de brilho (escala FFmpeg eq: -1.0 a 1.0, neutro=0)
    "saturation_range":  (-0.02, 0.02), # variação de saturação em torno de 1.0 (neutro FFmpeg eq)
    "zoom_range":        (1.01, 1.03),  # fator de zoom leve aplicado antes de compor no fundo
    "flip_chance":       0.5,           # probabilidade de flip horizontal (0.0 a 1.0)

    # Comportamento do script
    "skip_existing": True,  # pular vídeos com estado "sucesso" salvo OU arquivo de saída existente
    "min_duration":  3.0,   # duração mínima aceitável após trim (segundos)
}

# ─── PATHS ────────────────────────────────────────────────────────────────────
DIR_BASE    = Path(__file__).parent
DIR_ENTRADA = DIR_BASE / "entrada"
DIR_SAIDA   = DIR_BASE / "saida"
DIR_LOGS    = DIR_SAIDA / "logs"
FUNDO_PATH  = DIR_BASE / "fundo.png"
LOG_FILE    = DIR_LOGS / "processamento.log"
STATE_FILE  = DIR_SAIDA / "processados.json"  # persiste entre execuções

console = Console()


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


# ─── VALIDAÇÃO ────────────────────────────────────────────────────────────────

def validar_ambiente(logger: logging.Logger) -> list[Path]:
    """
    Valida FFmpeg, fundo.png e pastas.
    Retorna lista de .mp4 em /entrada ordenada alfabeticamente.
    Aborta com SystemExit em qualquer falha crítica.
    """
    # FFmpeg e ffprobe no PATH
    for cmd in ["ffmpeg", "ffprobe"]:
        try:
            subprocess.run([cmd, "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(f"{cmd} não encontrado no PATH.")
            logger.error("Windows: https://ffmpeg.org/download.html → extrair e adicionar pasta /bin ao PATH")
            logger.error("Mac:     brew install ffmpeg")
            logger.error("Linux:   sudo apt install ffmpeg")
            sys.exit(1)

    # fundo.png existe
    if not FUNDO_PATH.exists():
        logger.error(f"fundo.png ausente: {FUNDO_PATH}")
        logger.error(
            f"Adicione um arquivo fundo.png de "
            f"{CONFIG['canvas_width']}x{CONFIG['canvas_height']}px na raiz do script."
        )
        sys.exit(1)

    # Dimensões exatas do fundo via ffprobe
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

    # Criar pastas necessárias
    DIR_ENTRADA.mkdir(exist_ok=True)
    DIR_SAIDA.mkdir(exist_ok=True)

    # Listar e ordenar vídeos — suffix.lower() pega .mp4 e .MP4 no Windows
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

def obter_duracao(path: Path) -> float:
    """Retorna duração do vídeo em segundos via ffprobe."""
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
    data = json.loads(result.stdout)

    # format.duration é mais confiável; fallback para stream
    duration = data.get("format", {}).get("duration")
    if not duration:
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and "duration" in stream:
                duration = stream["duration"]
                break
    if not duration:
        raise ValueError(f"Não foi possível determinar duração de {path.name}")
    return float(duration)


# ─── ANTI-BAN ─────────────────────────────────────────────────────────────────

def gerar_params_antiban(seed: int) -> dict:
    """
    Gera parâmetros de variação aleatórios para um vídeo.
    Seed derivada do nome do arquivo: mesma seed = mesmos parâmetros (reproducível).
    """
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


# ─── CONSTRUÇÃO DOS FILTROS ───────────────────────────────────────────────────

def construir_filtros(params: dict) -> tuple[str, str]:
    """
    Monta filter_complex e audio_filter para FFmpeg.
    Aplica: speed, zoom, crop, brilho, saturação, flip opcional, overlay no fundo.
    Retorna (filter_complex_str, audio_filter_str).
    """
    canvas_w   = CONFIG["canvas_width"]
    canvas_h   = CONFIG["canvas_height"]
    video_w    = CONFIG["video_width"]
    speed      = params["speed"]
    brightness = params["brightness"]
    # eq filter: saturation neutro = 1.0; soma o delta configurado
    saturation = 1.0 + params["saturation"]
    zoom       = params["zoom"]
    flip       = params["flip"]

    # Largura com zoom — garantir múltiplo de 2 (libx264 exige)
    zoomed_w = int(video_w * zoom)
    if zoomed_w % 2 != 0:
        zoomed_w += 1

    # Cadeia de filtros de vídeo aplicados antes do overlay
    video_filters = [
        f"setpts=PTS/{speed}",                                        # aceleração temporal
        f"scale={zoomed_w}:-2",                                       # redimensiona com zoom (altura automática par)
        f"crop={video_w}:ih",                                         # recorta de volta à largura alvo → efeito zoom
        f"eq=brightness={brightness}:saturation={saturation:.4f}",    # brilho e saturação
    ]
    if flip:
        video_filters.append("hflip")                                 # flip horizontal — muda hash radicalmente

    # Posição X do overlay
    pos_x_cfg = CONFIG["position_x"]
    if pos_x_cfg == "center":
        overlay_x = f"({canvas_w}-overlay_w)/2"
    else:
        overlay_x = str(int(pos_x_cfg))

    # Posição Y do overlay — safe zone configurada em position_y
    overlay_y = int(canvas_h * CONFIG["position_y"])

    filter_complex = (
        f"[0:v]scale={canvas_w}:{canvas_h}[bg];"           # escala fundo ao canvas
        f"[1:v]{','.join(video_filters)}[vid];"             # processa vídeo
        f"[bg][vid]overlay={overlay_x}:{overlay_y}[out]"   # compõe overlay
    )

    # atempo sincroniza áudio com a aceleração do vídeo (range válido: 0.5–2.0)
    audio_filter = f"atempo={speed}"

    return filter_complex, audio_filter


# ─── PROGRESSO FFmpeg ─────────────────────────────────────────────────────────

def _monitorar_progresso(stdout_pipe, pbar: tqdm, duracao: float):
    """
    Thread auxiliar: lê output de progresso do FFmpeg (pipe:1) e atualiza tqdm.
    FFmpeg reporta out_time_ms a cada stats_period segundos.
    """
    for line in stdout_pipe:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                ms = int(line.split("=", 1)[1])
                if ms > 0:
                    pbar.n = min(ms / 1_000_000, duracao)
                    pbar.refresh()
            except ValueError:
                pass


# ─── RENDERIZAÇÃO ─────────────────────────────────────────────────────────────

def renderizar_video(
    input_path: Path,
    output_path: Path,
    params: dict,
    duracao_original: float,
    logger: logging.Logger,
) -> bool:
    """
    Executa FFmpeg single-pass: fundo.png + vídeo fonte → saída com anti-ban.
    Sem arquivos temporários. Sem MoviePy.
    Retorna True se sucesso, False se erro.
    """
    filter_complex, audio_filter = construir_filtros(params)
    trim_start  = params["trim_start"]
    duracao_saida = (duracao_original - trim_start) / params["speed"]

    cmd = [
        "ffmpeg", "-y",
        "-progress", "pipe:1",       # progresso estruturado para stdout (lido pela thread)
        "-stats_period", "0.5",      # frequência de atualização de progresso
        "-loop", "1",                # fundo.png em loop (imagem estática)
        "-i", str(FUNDO_PATH),       # input 0: fundo
        "-ss", str(trim_start),      # trim início antes do input (fast seek)
        "-i", str(input_path),       # input 1: vídeo fonte
        "-t", str(duracao_saida),    # duração total de saída
        "-filter_complex", filter_complex,
        "-filter:a", audio_filter,
        "-map", "[out]",             # stream de vídeo composto
        "-map", "1:a?",              # áudio original (opcional — evita erro em vídeo mudo)
        "-c:v", "libx264",
        "-crf", str(CONFIG["output_crf"]),
        "-preset", CONFIG["output_preset"],
        "-pix_fmt", "yuv420p",       # compatibilidade mobile obrigatória
        "-r", str(CONFIG["output_fps"]),
        "-c:a", "aac",
        "-b:a", CONFIG["audio_bitrate"],
        "-map_metadata", "-1",       # strip todos os metadados do container
        "-fflags", "+bitexact",      # remove fingerprint do encoder
        "-movflags", "+faststart",   # moov atom no início (streaming mobile/web)
        str(output_path),
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        stderr_lines: list[str] = []

        def _ler_stderr(pipe):
            for line in pipe:
                stderr_lines.append(line)

        with tqdm(
            total=duracao_saida,
            desc="  Renderizando",
            unit="s",
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n:.1f}/{total:.1f}s [{elapsed}<{remaining}]",
            ncols=72,
        ) as pbar:
            # Duas threads separadas: stdout (progresso) e stderr (erros)
            # process.communicate() não pode ser usado aqui pois conflita com leitura de stdout
            t_progress = threading.Thread(
                target=_monitorar_progresso,
                args=(process.stdout, pbar, duracao_saida),
                daemon=True,
            )
            t_stderr = threading.Thread(
                target=_ler_stderr,
                args=(process.stderr,),
                daemon=True,
            )
            t_progress.start()
            t_stderr.start()
            process.wait()
            t_progress.join(timeout=2)
            t_stderr.join(timeout=5)
            pbar.n = pbar.total
            pbar.refresh()

        stderr = "".join(stderr_lines)

        if process.returncode != 0:
            linhas_erro = [l for l in stderr.splitlines() if "error" in l.lower()]
            msg = linhas_erro[-1] if linhas_erro else (
                stderr.splitlines()[-1] if stderr else "erro desconhecido"
            )
            logger.error(f"FFmpeg falhou (código {process.returncode}): {msg}")
            return False

        return True

    except Exception as exc:
        logger.error(f"Exceção ao executar FFmpeg: {exc}")
        return False


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
    """Persiste histórico de processamento em JSON após cada vídeo."""
    STATE_FILE.write_text(
        json.dumps(estado, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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
    Orquestra processamento completo de um vídeo:
    validação de duração → params anti-ban → renderização → atualização de estado.
    Retorna: "sucesso" | "pulado" | "erro"
    """
    nome = video_path.name
    output_path = DIR_SAIDA / f"{video_path.stem}_edited.mp4"

    console.rule(f"[bold cyan]{index}/{total}: {nome}[/bold cyan]")

    # Skip por estado persistido (processamento anterior bem-sucedido)
    if CONFIG["skip_existing"] and estado.get(nome, {}).get("status") == "sucesso":
        logger.info(f"[yellow]PULADO[/yellow] {nome} — já processado (estado salvo)")
        return "pulado"

    # Skip por arquivo existente sem registro de estado
    if CONFIG["skip_existing"] and output_path.exists() and nome not in estado:
        logger.info(f"[yellow]PULADO[/yellow] {nome} — arquivo de saída já existe")
        return "pulado"

    # Obter duração
    try:
        duracao = obter_duracao(video_path)
    except ValueError as exc:
        logger.error(f"[red]ERRO[/red] {nome} — {exc}")
        return "erro"

    # Validar duração mínima após trim
    duracao_util = duracao - CONFIG["trim_start"]
    if duracao_util < CONFIG["min_duration"]:
        logger.warning(
            f"[yellow]PULADO[/yellow] {nome} — duração útil {duracao_util:.1f}s "
            f"< mínimo configurado {CONFIG['min_duration']}s"
        )
        return "pulado"

    # Parâmetros anti-ban (seed derivada do nome = mesma seed em reruns)
    seed = abs(hash(nome)) % (2 ** 31)
    params = gerar_params_antiban(seed)

    flip_str = "SIM" if params["flip"] else "NÃO"
    logger.info(
        f"Anti-ban — seed={seed} | speed={params['speed']}x | zoom={params['zoom']}x | "
        f"flip={flip_str} | brightness={params['brightness']:+.4f} | saturation={params['saturation']:+.4f}"
    )

    if dry_run:
        logger.info(f"[dim]DRY-RUN: renderização de {nome} não executada[/dim]")
        return "pulado"

    # Renderizar
    t_inicio = time.time()
    sucesso = renderizar_video(video_path, output_path, params, duracao, logger)
    tempo = round(time.time() - t_inicio, 1)

    if sucesso:
        logger.info(f"[green]OK[/green] {nome} → {output_path.name} ({tempo}s)")
        estado[nome] = {
            "status":    "sucesso",
            "output":    output_path.name,
            "tempo_s":   tempo,
            "params":    params,
            "timestamp": datetime.now().isoformat(),
        }
    else:
        logger.error(f"[red]ERRO[/red] {nome} — falhou após {tempo}s")
        estado[nome] = {
            "status":    "erro",
            "timestamp": datetime.now().isoformat(),
        }
        # Remover saída parcial ou corrompida
        if output_path.exists():
            output_path.unlink()

    salvar_estado(estado)
    return "sucesso" if sucesso else "erro"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    """Ponto de entrada. Loop principal com try/except individual por vídeo."""
    parser = argparse.ArgumentParser(
        description="Clonador de Reels — edição em lote 9:16 (Instagram/TikTok)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista vídeos e parâmetros anti-ban sem renderizar nada",
    )
    parser.add_argument(
        "--reprocess",
        metavar="ARQUIVO",
        help="Força reprocessar um arquivo específico ignorando skip_existing",
    )
    args = parser.parse_args()

    logger = setup_logging()

    console.rule("[bold magenta]Clonador de Reels — Dark Page[/bold magenta]")
    logger.info(f"Sessão iniciada — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        logger.info("[yellow]DRY-RUN ativo — nenhum vídeo será renderizado[/yellow]")

    videos = validar_ambiente(logger)
    estado = carregar_estado()

    # --reprocess: isola um arquivo e limpa estado anterior dele
    if args.reprocess:
        alvo = Path(args.reprocess).name
        videos = [v for v in videos if v.name == alvo]
        if not videos:
            logger.error(f"'{alvo}' não encontrado em /entrada.")
            sys.exit(1)
        estado.pop(alvo, None)
        salvar_estado(estado)
        logger.info(f"--reprocess: forçando '{alvo}' (estado anterior removido)")

    total = len(videos)
    contadores = {"sucesso": 0, "pulado": 0, "erro": 0}

    for i, video_path in enumerate(videos, start=1):
        try:
            resultado = processar_video(
                video_path=video_path,
                index=i,
                total=total,
                estado=estado,
                logger=logger,
                dry_run=args.dry_run,
            )
            contadores[resultado] += 1
        except Exception as exc:
            logger.exception(f"Erro inesperado em {video_path.name}: {exc}")
            contadores["erro"] += 1

    # Resumo final
    console.rule("[bold]Concluído[/bold]")
    tabela = Table(show_header=False, box=None, padding=(0, 2))
    tabela.add_row("[green]Sucesso[/green]",   f"[green]{contadores['sucesso']}[/green]")
    tabela.add_row("[yellow]Pulados[/yellow]",  f"[yellow]{contadores['pulado']}[/yellow]")
    tabela.add_row("[red]Erros[/red]",          f"[red]{contadores['erro']}[/red]")
    tabela.add_row("Total",                     str(total))
    console.print(tabela)

    logger.info(
        f"sucesso={contadores['sucesso']} | "
        f"pulados={contadores['pulado']} | "
        f"erros={contadores['erro']}"
    )


if __name__ == "__main__":
    main()
