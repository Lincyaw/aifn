// JSON leaves U+2028 and U+2029 unescaped inside strings, and node:readline ends a line at either, so a message that
// carries them, such as a file a tool read, would reach the other side cut in two.
const separators = new RegExp(`[${String.fromCharCode(0x2028, 0x2029)}]`, 'g');

export const jsonLine = value =>
  `${JSON.stringify(value).replace(separators, character => `\\u${character.charCodeAt(0).toString(16)}`)}\n`;
