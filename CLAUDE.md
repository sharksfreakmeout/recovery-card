# Recovery Card

**PROJECT:** Recovery Card. A macOS tool for the Build with Gemma hackathon, On-Device track. It helps a person recover context after an interruption: it quietly screenshots their work, and when they return it uses a local Gemma 4 model to generate a "Recovery Card" saying what they were doing, the reasoning behind it, and the next action.

## HARD RULES

1. **Everything runs 100% locally.** Model: `gemma4:12b-it-qat` via the Ollama API at `localhost:11434` (fallback: `gemma4:e2b-it-qat`). Never use any cloud API, hosted model, or cloud model tag. Never suggest Next.js, Vertex, AI Studio, or Hugging Face hosted inference. This is a disqualification risk.
2. **I am non-technical.** After every step, explain what you did and how to test it in plain English, with exact commands.
3. **Build one milestone at a time.** Stop after each and wait for my confirmation.
4. **Commit after every working milestone** with a clear message. Clean commit history is a judging requirement.
5. **Keep it simple:** Python 3, one small Flask page for the UI, no databases.
6. **If I paste an error:** diagnose in plain English, fix it, then give me one command to verify.
