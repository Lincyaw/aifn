import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { FileSystem, FsError } from '@deepseek-ai/dsh-fs';
import { contains } from './policy.mjs';

export default class SrtFileSystem extends FileSystem {
  static inject = ['aifnSrt'];
  constructor(ctx) { super(ctx); this.boundary = ctx.aifnSrt; this.boundary.markReady('fs'); }
  resolve(value, opts = {}) { return this.boundary.rpc('resolve', [value, { cwd: opts.cwd }], opts.signal); }
  processPath(target) { return String(target.targetKey); }
  processPathFromHostPath(value) {
    return path.isAbsolute(value) && this.boundary.readable.some(root => contains(root, value)) ? value : undefined;
  }
  fileUrl(target) { return pathToFileURL(this.processPath(target)).href; }
  contains(parent, child) { return contains(this.processPath(parent), this.processPath(child)); }
  stat(target, signal) { return this.boundary.rpc('stat', [target], signal); }
  lstat(value, opts, signal) { return this.boundary.rpc('lstat', [value, opts ?? {}], signal); }
  readText(target, signal) { return this.boundary.rpc('readText', [target], signal); }
  async readBytes(target, signal, maxBytes) {
    return Buffer.from((await this.boundary.rpc('readBytes', [target, maxBytes], signal)).bytes, 'base64');
  }
  async readByteRange(target, range, signal) {
    return Buffer.from((await this.boundary.rpc('readByteRange', [target, range], signal)).bytes, 'base64');
  }
  async *streamText(target, signal) {
    const decoder = new TextDecoder('utf-8', { fatal: true });
    for (let offset = 0;; offset += 65536) {
      const bytes = await this.readByteRange(target, { offset, length: 65536 }, signal);
      if (!bytes.length) break;
      if (offset === 0 && bytes.subarray(0, 8192).includes(0)) throw new FsError('File is not text', 'FS_NOT_TEXT');
      yield decoder.decode(bytes, { stream: true });
      if (bytes.length < 65536) break;
    }
    const tail = decoder.decode(); if (tail) yield tail;
  }
  listDir(target, signal) { return this.boundary.rpc('listDir', [target], signal); }
  writeText(target, content, expected, signal) { return this.boundary.rpc('writeText', [target, content, expected], signal); }
  editText(target, edit, expected, signal) { return this.boundary.rpc('editText', [target, edit, expected], signal); }
  async watch(target, changed, signal) {
    let version = (await this.stat(target, signal))?.version, pending = false;
    const timer = setInterval(async () => {
      if (pending) return; pending = true;
      try {
        const next = (await this.stat(target, signal))?.version;
        if (next !== version) { version = next; changed(); }
      } catch (error) { changed(error); } finally { pending = false; }
    }, 1000);
    timer.unref();
    return async () => { clearInterval(timer); };
  }
}
