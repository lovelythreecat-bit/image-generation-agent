# Configurable API design

The deployment selects named services (base URL, secret or explicit environment reference,
wire protocol and optional endpoint paths), image aliases, and vision stage routes.
JSON files and Python mappings enter the same Pydantic validation boundary. A database
adapter can supply that mapping; the core does not read a database or depend on one.

Explicit AgentConfig objects win; CLI --config loads a file, otherwise legacy
IMAGE_AGENT_* variables supply the existing OpenRouter configuration. Explicit structured
configuration never merges old environment settings. Only api_key_env references read
environment variables. Every route must name an existing service of the correct protocol;
credentials for the selected image alias and all vision routes are checked before
client creation or output reservation. Unselected image aliases need no credential;
analyze-only checks vision routes. Image aliases may be configured individually.

Image protocols: openrouter_images uses JSON /images; openai_images uses multipart
/images/edits with ordered image[] files or JSON /images/generations without references.
Vision uses chat_completions JSON. A bound service transport owns credentials and URL;
the pipeline keeps request/result contracts and no provider fallback is introduced.
Legacy style-model fallback remains for legacy configurations only.

Official example pins gpt-image-2 and gpt-4o-mini. GPT Image 2 sizes use integer
multiples of 16, aspect <=3:1, long edge <=3840 and 655360..8294400 pixels.
1K/2K/4K choose quality low/medium/high; the output canvas is independent. Unsupported
aspect ratios fail as capability errors. Do not send input_fidelity to GPT Image 2.

Preserve current output/error fixes and user files. Verification is offline with
httpx.MockTransport; no billable calls or claims of real provider acceptance.
