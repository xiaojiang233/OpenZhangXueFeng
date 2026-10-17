/** Small, deliberately safe Markdown renderer. Untrusted content is always text. */
export function safeUrl(raw) {
  if (typeof raw !== 'string') return null;
  try {
    const url = new URL(raw.trim());
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}
function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}
function link(label, url) {
  const node = el('a', label);
  node.href = url;
  node.target = '_blank';
  node.rel = 'noopener noreferrer';
  return node;
}

function inline(text, citations, depth = 0) {
  const fragment = document.createDocumentFragment();
  if (depth > 5) {
    fragment.append(document.createTextNode(text));
    return fragment;
  }
  // A token is parsed once; generated nodes never pass through string replacement.
  const pattern =
    /`([^`\n]+)`|!?\[([^\]\n]+)\]\(([^\s)]+)(?:\s+"[^"]*")?\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|~~([^~\n]+)~~|\*([^*\n]+)\*|\[(\d+(?:[,，\s-]+\d+)*)\]/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    fragment.append(document.createTextNode(text.slice(cursor, match.index)));
    if (match[1] !== undefined) fragment.append(el('code', match[1]));
    else if (match[2] !== undefined) {
      const url = safeUrl(match[3]);
      fragment.append(url ? link(match[2], url) : document.createTextNode(match[2]));
    } else if (match[4] || match[5] || match[6] || match[7]) {
      const node = el(match[6] ? 'del' : match[7] ? 'em' : 'strong');
      node.append(inline(match[4] || match[5] || match[6] || match[7], citations, depth + 1));
      fragment.append(node);
    } else {
      const numbers = new Set();
      for (const part of match[8].matchAll(/(\d+)(?:\s*-\s*(\d+))?/g)) {
        const first = Number(part[1]);
        const last = Number(part[2] || first);
        for (let number = Math.max(1, first); number <= Math.min(last, citations.length); number++)
          numbers.add(number);
      }
      if (!numbers.size) fragment.append(document.createTextNode(match[0]));
      numbers.forEach((number) => {
        const cite = citations[number - 1];
        const url = safeUrl(cite?.url);
        if (!url) {
          fragment.append(document.createTextNode(`[${number}]`));
          return;
        }
        const badge = link(String(number), url);
        badge.className = 'cite-badge';
        badge.title = [cite.title || url, cite.snippet].filter(Boolean).join('\n');
        badge.setAttribute('aria-label', `来源 ${number}：${cite.title || url}`);
        fragment.append(badge);
      });
    }
    cursor = match.index + match[0].length;
  }
  fragment.append(document.createTextNode(text.slice(cursor)));
  return fragment;
}

export function renderMarkdown(text, citations = []) {
  const output = document.createDocumentFragment();
  const lines = String(text || '')
    .slice(0, 240000)
    .replace(/\r\n?/g, '\n')
    .split('\n');
  let i = 0;
  const add = (tag, value) => {
    const node = el(tag);
    node.append(inline(value, citations));
    output.append(node);
  };
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (/^\s*```/.test(line)) {
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;
      const pre = el('pre');
      pre.append(el('code', code.join('\n')));
      output.append(pre);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+)/);
    if (heading) {
      add(`h${Math.min(heading[1].length + 1, 4)}`, heading[2]);
      i++;
      continue;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      output.append(el('hr'));
      i++;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i]))
        quote.push(lines[i++].replace(/^\s*>\s?/, ''));
      add('blockquote', quote.join('\n'));
      continue;
    }
    if (/^\s*(?:[-+*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = el(ordered ? 'ol' : 'ul');
      const pattern = ordered ? /^\s*\d+[.)]\s+(.+)/ : /^\s*[-+*]\s+(.+)/;
      while (i < lines.length) {
        const match = lines[i].match(pattern);
        if (!match) break;
        const li = el('li');
        li.append(inline(match[1], citations));
        list.append(li);
        i++;
      }
      output.append(list);
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const table = el('table');
      const split = (row) =>
        row
          .trim()
          .replace(/^\||\|$/g, '')
          .split('|')
          .map((cell) => cell.trim());
      const head = el('thead');
      const tr = el('tr');
      split(line).forEach((cell) => {
        const th = el('th');
        th.scope = 'col';
        th.append(inline(cell, citations));
        tr.append(th);
      });
      head.append(tr);
      table.append(head);
      i += 2;
      const body = el('tbody');
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        const row = el('tr');
        split(lines[i++]).forEach((cell) => {
          const td = el('td');
          td.append(inline(cell, citations));
          row.append(td);
        });
        body.append(row);
      }
      table.append(body);
      output.append(table);
      continue;
    }
    const paragraph = el('p');
    do {
      if (paragraph.childNodes.length) paragraph.append(el('br'));
      paragraph.append(inline(lines[i++], citations));
    } while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(?:#{1,6}\s|```|>|[-+*]\s|\d+[.)]\s|[-*_]{3,}\s*$)/.test(lines[i]) &&
      !(lines[i].includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || ''))
    );
    output.append(paragraph);
  }
  return output;
}

export function renderCitations(citations = []) {
  const sources = citations
    .map((cite, index) => ({ cite, index, url: safeUrl(cite?.url) }))
    .filter((item) => item.url);
  if (!sources.length) return document.createDocumentFragment();
  const section = el('div');
  section.className = 'citation-list';
  const title = el('div', '参考来源 · 请核实最新官方信息');
  title.className = 'citation-title';
  section.append(title);
  sources.forEach(({ cite, index, url }) => {
    const a = link('', url);
    a.className = 'citation-link';
    const number = el('span', String(index + 1));
    number.className = 'citation-number';
    a.append(number, el('span', cite.title || new URL(url).hostname));
    a.title = cite.snippet || url;
    section.append(a);
  });
  return section;
}
