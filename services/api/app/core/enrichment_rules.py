"""Centralized deterministic rules used by content enrichment."""

CATEGORY_PRIORITY = (
    "engenharia_de_prompt",
    "automacao",
    "marketing",
    "negocios",
    "inteligencia_artificial",
    "tecnologia",
)

CATEGORY_RULES = {
    "engenharia_de_prompt": ("prompt engineering", "engenharia de prompt", "prompt", "prompts", "system prompt", "few shot", "zero shot"),
    "automacao": ("automation", "automacao", "workflow", "workflows", "agent", "agents", "agente", "agentes", "zapier", "make.com", "n8n"),
    "marketing": ("marketing", "social media", "instagram", "copywriting", "conteudo", "content marketing", "growth", "branding"),
    "negocios": ("business", "negocio", "negocios", "startup", "empreendedorismo", "vendas", "sales", "monetizacao", "receita"),
    "inteligencia_artificial": ("artificial intelligence", "inteligencia artificial", "machine learning", "deep learning", "generative ai", "ia generativa", "chatgpt", "openai", "claude", "gemini", "llm", "large language model"),
    "tecnologia": ("technology", "tecnologia", "software", "programacao", "programming", "developer", "desenvolvedor", "api", "cloud"),
}

TOPIC_RULES = {
    "chatgpt": ("chatgpt", "gpt-4", "gpt-5"),
    "openai": ("openai",),
    "claude": ("claude", "anthropic"),
    "gemini": ("gemini", "google ai"),
    "agentes": ("ai agent", "agents", "agentic", "agente", "agentes"),
    "automacao": ("automation", "automacao", "workflow", "n8n", "zapier", "make.com"),
    "engenharia_de_prompt": ("prompt engineering", "engenharia de prompt", "system prompt", "few shot", "zero shot"),
    "instagram": ("instagram", "reels", "stories"),
    "marketing": ("marketing", "branding", "copywriting", "growth"),
}

STOPWORDS = frozenset({"de", "da", "do", "das", "dos", "para", "com", "em", "um", "uma", "o", "a", "os", "as", "e", "the", "and", "for", "with", "from", "this", "that", "into", "your", "you", "are", "was", "como", "por", "sobre", "mais", "menos"})

AI_TERMS = CATEGORY_RULES["inteligencia_artificial"]
PROMPT_TERMS = CATEGORY_RULES["engenharia_de_prompt"]
AUTOMATION_TERMS = CATEGORY_RULES["automacao"]
MARKETING_TERMS = CATEGORY_RULES["marketing"]
