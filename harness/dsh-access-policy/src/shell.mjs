import { LocalBashExecutor } from '@deepseek-ai/dsh-bash-local';

export default class SrtBashExecutor extends LocalBashExecutor {
  static inject = ['subprocess', 'aifnSrt'];
  constructor(ctx, config) { super(ctx, config); this.boundary = ctx.aifnSrt; this.boundary.markReady('shell'); }
  spawnSpec(...args) {
    const spec = super.spawnSpec(...args);
    // Host spill directories are private; return a bounded tail instead of an unreadable spill path.
    for (const name of ['stdout', 'stderr']) {
      if (typeof spec.stdio[name] === 'object') delete spec.stdio[name].spill;
    }
    spec.env = { ...Object.fromEntries(Object.keys(process.env).map(key => [key, undefined])), ...this.boundary.environment };
    return spec;
  }
  async execute(spec) {
    return this.executeArgv({ ...spec, workdir: this.boundary.policy.cwd }, signal =>
      this.boundary.wrap([this.boundary.tools.bash, '-c', spec.command], signal, spec.workdir));
  }
}
