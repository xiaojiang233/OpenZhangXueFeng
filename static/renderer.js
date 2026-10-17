import { icon } from './icons.js';
import { renderMarkdown, renderCitations } from './markdown.js';

export const dom = Object.fromEntries(
  [...document.querySelectorAll('[id]')].map((node) => [node.id, node]),
);
export function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
export function toast(text, error = false) {
  const element = node('div', `toast${error ? ' error' : ''}`, text);
  dom.toastStack.append(element);
  setTimeout(() => element.remove(), error ? 6000 : 3500);
}

export function renderMessage(record) {
  const article = node('article', `message ${record.role}${record.error ? ' error' : ''}`);
  const avatar = node(
    'div',
    'msg-avatar',
    record.error ? '!' : record.role === 'user' ? '你' : '峰',
  );
  avatar.setAttribute('aria-hidden', 'true');
  const content = node('div', 'msg-content');
  const meta = node(
    'div',
    'msg-meta',
    record.role === 'user' ? '你' : record.error ? '服务提示' : '张雪峰',
  );
  if (record.role !== 'user' && !record.error) meta.append(node('span', '', 'AI'));
  const bubble = node('div', 'msg-bubble');
  if (record.role === 'user' || record.error) bubble.textContent = record.content;
  else {
    bubble.append(renderMarkdown(record.content, record.citations));
    if (!record.streaming) bubble.append(renderCitations(record.display_citations || []));
  }
  bubble.classList.toggle('streaming-cursor', Boolean(record.streaming));
  content.append(meta, bubble);
  if (record.role === 'assistant' && !record.streaming && !record.error) {
    const actions = node('div', 'message-actions');
    const copy = node('button', 'copy-button');
    copy.type = 'button';
    copy.append(icon('copy'), document.createTextNode('复制'));
    copy.setAttribute('aria-label', '复制这条回答');
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(record.content);
        toast('回答已复制');
      } catch {
        toast('无法自动复制，请选择文字后复制', true);
      }
    });
    actions.append(copy);
    if (record.interrupted) actions.append(node('span', 'message-label', '已停止生成'));
    content.append(actions);
  }
  article.append(avatar, content);
  return article;
}

export class ConversationView {
  constructor() {
    this.items = [];
    this.streamNode = null;
    this.frame = null;
    this.follow = true;
    dom.chatArea.addEventListener(
      'scroll',
      () => {
        this.follow =
          dom.chatArea.scrollHeight - dom.chatArea.scrollTop - dom.chatArea.clientHeight < 100;
        dom.btnScrollLatest.hidden = this.follow || !this.items.length;
      },
      { passive: true },
    );
    dom.btnScrollLatest.addEventListener('click', () => this.scroll(true));
  }
  reset(records = []) {
    cancelAnimationFrame(this.frame);
    this.frame = null;
    this.streamNode = null;
    this.items = records;
    dom.messages.replaceChildren(...records.map(renderMessage));
    this.toggle();
    this.scroll(true);
  }
  toggle() {
    dom.chatEmpty.hidden = Boolean(this.items.length);
    dom.messages.hidden = !this.items.length;
  }
  append(record) {
    this.items.push(record);
    dom.messages.append(renderMessage(record));
    this.toggle();
    this.scroll();
  }
  update(record) {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      const index = this.items.indexOf(record);
      if (index < 0) return;
      const next = renderMessage(record);
      dom.messages.children[index]?.replaceWith(next);
      this.scroll();
    });
  }
  scroll(force = false) {
    if (force) this.follow = true;
    if (this.follow)
      requestAnimationFrame(() => {
        dom.chatArea.scrollTop = dom.chatArea.scrollHeight;
        dom.btnScrollLatest.hidden = true;
      });
    else dom.btnScrollLatest.hidden = false;
  }
}

function dateGroup(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '更早';
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const age =
    (start - new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()) / 86400000;
  return age < 1 ? '今天' : age < 2 ? '昨天' : age < 7 ? '近 7 天' : age < 30 ? '近 30 天' : '更早';
}

export function renderConversations(conversations, activeId, query, onLoad, onDelete) {
  const filtered = conversations.filter((item) =>
    String(item.title || '')
      .toLocaleLowerCase()
      .includes(query.trim().toLocaleLowerCase()),
  );
  dom.historyCount.textContent = String(conversations.length);
  dom.conversationList.replaceChildren();
  if (!filtered.length) {
    const empty = node('div', 'history-empty', query ? '没有找到相关对话' : '还没有对话');
    empty.append(
      document.createElement('br'),
      node('span', '', query ? '试试其他关键词' : '从一个好问题开始吧'),
    );
    dom.conversationList.append(empty);
    return;
  }
  let lastGroup = '';
  filtered.forEach((item) => {
    const group = dateGroup(item.updated_at);
    if (group !== lastGroup) {
      dom.conversationList.append(node('div', 'history-group', group));
      lastGroup = group;
    }
    const row = node('div', `conv-item${item.id === activeId ? ' active' : ''}`);
    const open = node('button', 'conv-open');
    open.type = 'button';
    open.append(icon('chat'), node('span', '', item.title || '新对话'));
    open.title = item.title || '新对话';
    if (item.id === activeId) open.setAttribute('aria-current', 'true');
    open.addEventListener('click', () => onLoad(item.id));
    const remove = node('button', 'conv-delete');
    remove.type = 'button';
    remove.append(icon('trash'));
    remove.setAttribute('aria-label', `删除对话：${item.title || '新对话'}`);
    remove.title = '删除对话';
    remove.addEventListener('click', () => onDelete(item));
    row.append(open, remove);
    dom.conversationList.append(row);
  });
}
