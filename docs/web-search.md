# Web search tool

AutoCode registers the read-only `web_search` tool when `TAVILY_API_KEY` is
available in the local Agent environment:

```dotenv
TAVILY_API_KEY=tvly-your-key
```

The tool uses Tavily basic search and returns bounded titles, URLs, relevance
scores, and snippets. Search results are untrusted external input: they are
provided to the model as reference material and do not execute commands or
write workspace files.

The public Relay does not need this key. Configure it only on the computer that
runs `autocode-web-runner`, then restart the Runner. If the key is absent, the
tool is not registered.

Reference: [Tavily Python SDK quick start](https://docs.tavily.com/sdk/python/quick-start).
