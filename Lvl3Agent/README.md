# Bank Agent App (Local LLM + MCP + n8n + FastAPI + UI)

This is a local AI assistant for commercial credit operations.  
It connects:
- A local LLM (via Ollama and GPT-OSS 20B)
- An MCP server exposing safe tools (read_file, get_pipeline_summary, debt_yield)
- n8n for live pipeline data
- FastAPI for the backend
- A simple browser chat UI

## Run Locally

1. Start n8n and confirm the `/webhook/pipeline_summary` endpoint works.
2. Start Ollama:
   ```bash
   ollama run gpt-oss:20b "hello"
