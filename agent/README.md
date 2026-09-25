# agent/ — permukaan agen brain-rsi (generasi 4)

Direktori ini (bersama `.claude/skills/`) adalah **satu-satunya** permukaan yang boleh
dimutasi oleh kandidat RSI (lihat `sources/registry.json`, source `brain-rsi`, role `target`).

Isi yang direncanakan — diturunkan dari generasi 1–3 setelah `wiki/lessons/` disetujui:

| File | Asal | Status |
|---|---|---|
| `PROMPT.md` | gen-2 `brains/brain/agent/PROMPT.md` + aturan operasional gen-3 (`memory/claude`, runbook) | belum diseed |
| `AGENT-OPERATING-LOOP.md` | gen-2 `agent/AGENT-OPERATING-LOOP.md` + gen-1 `AGENT-OPERATING-LOOP.md` | belum diseed |
| `RUNBOOK.md` | gen-2 `agent/RUNBOOK.md` + gen-3 `wikis/client-a/*runbook*` | belum diseed |

`CLAUDE.md` di root repo adalah **kontrak keamanan**, bukan prompt agen; ia sengaja di luar allowlist.
`wiki/`, `raw/`, `log/` adalah konten — tidak pernah bagian dari permukaan yang bisa dimutasi.
