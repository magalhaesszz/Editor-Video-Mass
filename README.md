# Clonador de Reels — Editor em Massa 9:16

Automação de edição em lote para **Instagram Reels** e **TikTok**. Sobrepõe vídeos em um fundo fixo (9:16), aplica técnicas anti-ban e exporta com qualidade profissional via FFmpeg.

---

## O que faz

- Processa todos os `.mp4` da pasta `/entrada` em ordem alfabética
- Sobrepõe cada vídeo sobre o `fundo.png` (1080x1920px)
- Aplica anti-ban automático por vídeo (speed, zoom, flip, brilho, saturação, trim, strip de metadados)
- Exporta em `/saida` com codec H.264, AAC 192k, yuv420p (compatível com mobile)
- Salva log completo em `/saida/logs/processamento.log`
- Persiste estado entre execuções — retoma de onde parou sem reprocessar

---

## Requisitos

### FFmpeg (obrigatório — instalar no sistema)

| SO | Comando |
|----|---------|
| Windows | [ffmpeg.org/download.html](https://ffmpeg.org/download.html) → extrair → adicionar `/bin` ao PATH |
| Mac | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

Verificar instalação:
```bash
ffmpeg -version
```

### Python 3.10+

```bash
pip install -r requirements.txt
```

---

## Estrutura de pastas

```
projeto/
├── clonador_reels.py   # script principal
├── requirements.txt
├── fundo.png           # seu template de fundo (1080x1920px) — obrigatório
├── entrada/            # coloque os .mp4 aqui
└── saida/
    ├── video_edited.mp4
    └── logs/
        └── processamento.log
```

---

## Como usar

### 1. Preparar

Coloque `fundo.png` (exatamente **1080x1920px**) na raiz do projeto.  
Coloque os vídeos `.mp4` na pasta `/entrada`.

### 2. Testar sem renderizar

```bash
python clonador_reels.py --dry-run
```

Lista vídeos encontrados e parâmetros anti-ban que seriam aplicados — sem processar nada.

### 3. Processar

```bash
python clonador_reels.py
```

### 4. Forçar reprocessar um arquivo específico

```bash
python clonador_reels.py --reprocess nome_do_video.mp4
```

---

## Técnicas anti-ban aplicadas

Cada vídeo recebe combinação única e aleatória baseada no nome do arquivo (reproducível):

| Técnica | Descrição |
|---------|-----------|
| **Trim** | Remove 0.1s do início — elimina frame inicial idêntico |
| **Speed** | Acelera entre 1.02x e 1.05x — muda duração e hash |
| **Zoom** | Zoom leve entre 1.01x e 1.03x — muda composição de pixels |
| **Flip** | 50% de chance de espelhar horizontalmente |
| **Brilho** | Variação de +1% a +2% aleatória |
| **Saturação** | Variação de -2% a +2% aleatória |
| **Strip metadata** | Remove todos os metadados do container (`-map_metadata -1 -fflags +bitexact`) |

---

## Configuração

Todos os parâmetros ficam no topo de `clonador_reels.py` no dicionário `CONFIG`:

```python
CONFIG = {
    "video_width":   800,        # largura do vídeo sobre o fundo (px)
    "position_y":    0.40,       # posição vertical (0.40 = 40% do topo)
    "output_crf":    18,         # qualidade: 18=alta, 23=média, 28=comprimida
    "output_preset": "slow",     # velocidade de encode vs qualidade
    "flip_chance":   0.5,        # probabilidade de flip (0.0 a 1.0)
    "skip_existing": True,       # pular vídeos já processados
    ...
}
```

> **Safe zones:**
> - Instagram Reels: UI cobre os últimos ~250px inferiores
> - TikTok: UI cobre os últimos ~300px inferiores
>
> Ajuste `position_y` para manter o conteúdo visível.

---

## Saída de exemplo no terminal

```
──────────── Clonador de Reels — Dark Page ────────────
Encontrados 12 vídeos para processar.

─────────────── 1/12: 01_video.mp4 ───────────────
Anti-ban — seed=847392 | speed=1.034x | zoom=1.021x | flip=SIM | brightness=+0.0142 | saturation=-0.0087
  Renderizando:  78%|████████  | 23.4/30.0s [00:45<00:12]
OK 01_video.mp4 → 01_video_edited.mp4 (58.3s)

──────────────────── Concluído ────────────────────
Sucesso    12
Pulados     0
Erros       0
Total      12
```

---

## Dependências Python

```
tqdm>=4.65.0
rich>=13.0.0
```

Pipeline 100% FFmpeg — sem MoviePy.
