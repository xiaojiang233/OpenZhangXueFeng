const paths = {
  settings: 'M4 7h16M4 17h16M8 4v6M16 14v6',
  plus: 'M12 5v14M5 12h14',
  search: 'M16 16l4 4M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0',
  chat: 'M20 11.5a8.5 8.5 0 0 1-8.5 8.5 10 10 0 0 1-4-.9L3 20l1-4.5a8.5 8.5 0 1 1 16-4ZM8 10h8M8 14h5',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  send: 'M12 19V5m-6 6 6-6 6 6',
  close: 'm6 6 12 12M6 18 18 6',
  menu: 'M4 6h16M4 12h16M4 18h16',
  mic: 'M9 6a3 3 0 0 1 6 0v5a3 3 0 0 1-6 0V6ZM5 10v1a7 7 0 0 0 14 0v-1M12 18v3M8 21h8',
  volume: 'm11 4-6 5H2v6h3l6 5V4ZM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14',
  phone: 'm4 3 4 1 1 5-2 2a17 17 0 0 0 6 6l2-2 5 1 1 4c-1 4-9 1-14-4S0 4 4 3Z',
  sun: 'M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5',
  help: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M9.5 9a2.5 2.5 0 0 1 5 .5c0 2-2.5 2-2.5 4M12 16.5v.5',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7',
  book: 'M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1v14',
  compass: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0m-5-4-2.5 5.5L8 16l2.5-5.5L16 8Z',
  case: 'M3 7h18v14H3V7ZM8 7V3h8v4M3 12a25 25 0 0 0 18 0M10 13v3h4v-3',
  cap: 'm2 8 10-5 10 5-10 5L2 8Zm4 2v7c4 3 8 3 12 0v-7M22 8v8',
  copy: 'M8 8h13v13H8V8Zm8 0V3H3v13h5',
  refresh: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3',
};
export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', paths[name] || paths.chat);
  svg.append(path);
  return svg;
}
export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((el) => el.replaceChildren(icon(el.dataset.icon)));
}
