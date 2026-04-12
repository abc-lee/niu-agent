# Niu Browser Server

MCP server for browser automation using Playwright.

## Installation

```bash
pip install -e .
playwright install chromium
```

## Usage

Use via MCP loader. The server provides 11 browser automation tools:

### Basic Tools
- `browser_navigate(url, wait_until)` - Navigate to URL
- `browser_screenshot()` - Take screenshot
- `browser_get_text()` - Extract page text
- `browser_click(selector)` - Click element
- `browser_fill(selector, text)` - Fill input field
- `browser_wait_for_selector(selector, timeout)` - Wait for element
- `browser_query_selector(selector)` - Check element exists
- `browser_fill_multiple({fields})` - Batch fill fields

### High-Level Tools
- `browser_fill_form(url, {field: value})` - Intelligent form filling
- `browser_answer_question(url, question)` - Answer question on page
- `browser_extract_data(url, {field: selector})` - Extract structured data

## Limitations

- No anti-bot bypass (CAPTCHA/Cloudflare may block)
- User must handle CAPTCHA manually
- No proxy rotation
- No session persistence

## License

MIT
