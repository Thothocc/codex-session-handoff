# Quickstart / 快速开始

## History map / 历史演化梳理

```bash
python3 codex_handoff.py --root ~/.codex/sessions history \
  /path/to/workspace \
  --history-depth balanced \
  -n 12 \
  -o ~/handoff_pack
```

Upload:

```text
~/handoff_pack/CHATGPT_HANDOFF_INPUT.md
```

Paste into ChatGPT:

```text
~/handoff_pack/NEXT_MESSAGE_PHASE1.txt
```

After reviewing Phase 1, paste:

```text
~/handoff_pack/NEXT_MESSAGE_PHASE2.txt
```

## Current handoff / 当前工作分支交接

```bash
python3 codex_handoff.py --root ~/.codex/sessions handoff \
  /path/to/focus \
  --workspace-root /path/to/workspace \
  -n 5 \
  -o ~/handoff_pack
```
