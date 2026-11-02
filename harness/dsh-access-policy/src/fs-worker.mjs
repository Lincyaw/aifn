import { createInterface } from 'node:readline';
import { Context } from '@deepseek-ai/cordis';
import { LocalFileSystem } from '@deepseek-ai/dsh-fs-local';

const fs = new LocalFileSystem(new Context(), { cwd: process.cwd(), diffBasisMaxBytes: 10 * 1024 * 1024 });
const running = new Map();
const methods = new Set(['resolve', 'stat', 'lstat', 'readText', 'readBytes', 'readByteRange', 'listDir', 'writeText', 'editText']);
const reply = message => process.stdout.write(JSON.stringify(message) + '\n');
reply({ ready: true });
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const request = JSON.parse(line);
  if (request.cancel !== undefined) { running.get(request.cancel)?.abort(); continue; }
  void (async () => {
    const { id, method, args } = request;
    const controller = new AbortController(); running.set(id, controller);
    try {
      if (!methods.has(method)) throw new Error('Unknown filesystem operation');
      const signal = controller.signal;
      let call;
      if (method === 'resolve') call = [args[0], { ...args[1], signal }];
      else if (method === 'readBytes') call = [args[0], signal, args[1]];
      else call = [...args, signal];
      const result = await fs[method](...call);
      reply({ id, value: result instanceof Uint8Array ? { bytes: Buffer.from(result).toString('base64') } : result });
    } catch (error) { reply({ id, error: { message: error.message, code: error.code } }); }
    finally { running.delete(id); }
  })();
}
await Promise.allSettled([...running.values()].map(controller => { controller.abort(); }));
