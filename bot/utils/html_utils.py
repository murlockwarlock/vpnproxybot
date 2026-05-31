"""HTML/Markdown utilities for safe formatting of AI responses in Telegram."""

from __future__ import annotations

import html
import re


def _fix_unclosed_html_tags(text: str) -> str:
    """Fully reconstructs HTML tags for correct nesting.

    Handles ALL kinds of invalid HTML:
    - Extra closing tags (</i> without <i>) → removed
    - Unclosed tags (<b> without </b>) → closed at end
    - Cross nesting (<b><i>...</b></i>) → order corrected
    - Duplicate tags (<b><b>...</b></b>) → duplicates skipped
    """
    allowed_tags = {'b', 'i', 's', 'code', 'pre', 'a'}
    tag_pattern = re.compile(r'<(/?)([a-z1-6]+)([^>]*)>', re.IGNORECASE)

    segments = []
    last_end = 0
    for match in tag_pattern.finditer(text):
        tag_name = match.group(2).lower()
        if tag_name not in allowed_tags:
            continue
        if match.start() > last_end:
            segments.append(('text', text[last_end:match.start()]))
        is_closing = bool(match.group(1))
        segments.append(('close' if is_closing else 'open', tag_name, match.group(0)))
        last_end = match.end()
    if last_end < len(text):
        segments.append(('text', text[last_end:]))

    result = []
    stack = []

    for seg in segments:
        if seg[0] == 'text':
            result.append(seg[1])
        elif seg[0] == 'open':
            tag_name = seg[1]
            if any(t[0] == tag_name for t in stack):
                continue
            stack.append((tag_name, seg[2]))
            result.append(seg[2])
        elif seg[0] == 'close':
            tag_name = seg[1]
            idx = None
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag_name:
                    idx = i
                    break
            if idx is None:
                continue
            tags_to_reopen = []
            while len(stack) > idx + 1:
                t = stack.pop()
                result.append(f'</{t[0]}>')
                tags_to_reopen.append(t)
            stack.pop()
            result.append(f'</{tag_name}>')
            for t in reversed(tags_to_reopen):
                stack.append(t)
                result.append(t[1])

    while stack:
        t = stack.pop()
        result.append(f'</{t[0]}>')

    return ''.join(result)


def markdown_to_html(text: str) -> str:
    """Convert Markdown / mixed Markdown+HTML to clean Telegram HTML."""
    if not text:
        return ""

    # Normalise any existing HTML tags back to markdown so we re-process uniformly
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<pre><code>(.*?)</code></pre>', r'```\1```', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)

    # Escape any remaining raw HTML special chars before we insert our own tags
    text = html.escape(text, quote=False)
    code_blocks: dict[str, str] = {}

    def save_code_block(match):
        key = f"\x01CODEBLOCK{len(code_blocks)}\x01"
        code_blocks[key] = match.group(1)
        return key

    def save_inline_code(match):
        key = f"\x01INLINE{len(code_blocks)}\x01"
        code_blocks[key] = match.group(1)
        return key

    # Save code blocks before any further processing
    text = re.sub(r'```(.*?)```', save_code_block, text, flags=re.DOTALL)
    text = re.sub(r'`(.*?)`', save_inline_code, text)

    # Horizontal rules
    text = re.sub(r'^\s*[\*_-]{3,}\s*$', '———', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\*\s*$', '———', text, flags=re.MULTILINE)

    # Bullet lists
    text = re.sub(r'^\s*[-*]\s+', '• ', text, flags=re.MULTILINE)

    # Headers → bold
    text = re.sub(
        r'^\s*#{1,6}\s+(.*)',
        lambda m: '\n\n<b>' + m.group(1).replace('***', '').replace('**', '').replace('*', '') + '</b>\n',
        text, flags=re.MULTILINE
    )

    # Bold+italic (***text*** or ___text___)
    text = re.sub(r'\*\*\*(?=[^<>]*\*\*\*)((?:(?!\n\n)[^<>])+?)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'___((?:(?!\n\n)[^<>])+?)___', r'<b><i>\1</i></b>', text)

    # Bold (**text** or __text__)
    text = re.sub(r'\*\*(?=[^<>]*\*\*)((?:(?!\n\n)[^<>])+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(?=[^<>]*__)((?:(?!\n\n)[^<>])+?)__', r'<b>\1</b>', text)

    # Italic (*text* or _text_)
    text = re.sub(r'(?<!\w)\*(?!\s)([^<>\n]+?)(?<!\s)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(?!\s)([^<>\n]+?)(?<!\s)_(?!\w)', r'<i>\1</i>', text)

    # Strikethrough
    text = re.sub(r'~~(?=[^<>\n]+~~)([^<>\n]+?)~~', r'<s>\1</s>', text)

    # Links
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)

    # Spacing fixes
    text = re.sub(r'([^\n])\n(<b>)', r'\1\n\n\2', text)
    text = re.sub(r'([^\n])\n(•)', r'\1\n\n\2', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Clean up leftover markdown symbols
    text = text.replace('**', '').replace('__', '').replace('~~', '')
    text = text.replace('<b></b>', '').replace('<i></i>', '')
    text = re.sub(r'(?<![\w*])\*(?![\w*])', '', text)

    # Restore code blocks
    for key, value in code_blocks.items():
        if "CODEBLOCK" in key:
            replacement = f'<pre><code>{value}</code></pre>'
        else:
            replacement = f'<code>{value}</code>'
        text = text.replace(key, replacement)

    text = text.strip()
    text = _fix_unclosed_html_tags(text)
    return text


def remove_markdown(text: str) -> str:
    """Strip all markdown formatting, return plain text."""
    text = re.sub(r'#+\s+', '', text)
    text = re.sub(r'\*\*(.*?)\*\*|__(.*?)__', r'\1', text)
    text = re.sub(r'\*(.*?)\*|_(.*?)_', r'\1', text)
    text = re.sub(r'~~(.*?)~~', r'\1', text)
    text = re.sub(r'`(.*?)`', r'\1', text)
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', text)
    return text


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split plain text into chunks that fit within Telegram's message limit."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""
    for paragraph in text.split('\n'):
        if len(current_chunk) + len(paragraph) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk)
            while len(paragraph) > max_length:
                split_at = paragraph.rfind(' ', 0, max_length)
                if split_at == -1:
                    split_at = max_length
                chunks.append(paragraph[:split_at])
                paragraph = paragraph[split_at:].lstrip()
            current_chunk = paragraph
        else:
            current_chunk = (current_chunk + "\n" + paragraph) if current_chunk else paragraph

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def split_html_text(text: str, max_length: int = 4090) -> list[str]:
    """Split HTML-formatted text into chunks, preserving open/close tag balance."""
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""
    open_tags: list[tuple[str, str]] = []

    tag_re = re.compile(r'(</?[a-z1-6]+(?: [^>]+)?>)', re.IGNORECASE)
    parts = tag_re.split(text)

    def _suffix() -> str:
        return "".join([f"</{t[0]}>" for t in reversed(open_tags)])

    def _reopen() -> str:
        return "".join([t[1] for t in open_tags])

    def _close_size() -> int:
        return sum(len(t[0]) + 3 for t in open_tags)

    for part in parts:
        if not part:
            continue

        if part.startswith('<'):
            tag_match = re.match(r'<(/?)([a-z1-6]+)', part, re.IGNORECASE)
            if tag_match:
                is_closing = bool(tag_match.group(1))
                tag_name = tag_match.group(2).lower()
                if is_closing:
                    if open_tags and open_tags[-1][0] == tag_name:
                        open_tags.pop()
                elif tag_name not in ['br', 'hr', 'img']:
                    open_tags.append((tag_name, part))

            suffix = _suffix()
            if len(current_chunk) + len(part) + len(suffix) > max_length:
                if current_chunk.strip():
                    chunks.append(current_chunk + suffix)
                current_chunk = _reopen()

            current_chunk += part
        else:
            while len(current_chunk) + len(part) + _close_size() > max_length:
                remaining_space = max_length - len(current_chunk) - _close_size()

                if remaining_space <= 10:
                    suffix = _suffix()
                    if current_chunk.strip():
                        chunks.append(current_chunk + suffix)
                    current_chunk = _reopen()
                    remaining_space = max_length - len(current_chunk) - _close_size()

                split_at = remaining_space
                for separator in ('\n\n', '. ', '! ', '? ', '\n'):
                    found_at = part.rfind(separator, 0, remaining_space)
                    if found_at != -1 and found_at > (remaining_space // 3):
                        split_at = found_at + len(separator.rstrip())
                        break
                else:
                    split_at = part.rfind(' ', 0, remaining_space)
                    if split_at == -1:
                        split_at = remaining_space

                suffix = _suffix()
                if (current_chunk + part[:split_at]).strip():
                    chunks.append(current_chunk + part[:split_at] + suffix)

                current_chunk = _reopen()
                part = part[split_at:].lstrip()

            current_chunk += part

    if current_chunk.strip():
        clean_chunk = re.sub(r'(<[a-z1-6][^>]*>)+$', '', current_chunk)
        if clean_chunk.strip():
            suffix = _suffix()
            final_c = clean_chunk + suffix
            if re.sub(r'<[^>]+>', '', final_c).strip():
                chunks.append(final_c)

    return [c for c in chunks if re.sub(r'<[^>]+>', '', c).strip()]
