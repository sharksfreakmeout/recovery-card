# Recovery Card

**PROJECT:** Recovery Card. A macOS tool for the Build with Gemma hackathon, On-Device track. It passively captures screenshots of a person's work, and after an interruption uses a local Gemma 4 model to infer the cognitive state behind the work: what they were doing, why, and what comes next.

## HARD RULES

1. **100% local at runtime.** Model `gemma4:12b-it-qat` via Ollama at `localhost:11434`, fallback `gemma4:e2b-it-qat`. Never any cloud API, hosted model, or network call. Disqualification risk.
2. **One milestone at a time**, wait for my confirmation.
3. **Commit every working milestone** with a clear message and push.
4. **Python 3 + Flask only.** No databases.
5. **On errors:** diagnose in plain English, fix, give me one verify command.
6. **Never delete files, install system-wide software, or run destructive git commands** without asking me.

## Working style

I am non-technical. After every step, explain what you did and how to test it in plain English, with exact commands.
