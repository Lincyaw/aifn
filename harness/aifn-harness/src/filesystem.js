import { existsSync, realpathSync } from 'node:fs'
import { join } from 'node:path'
import { FsError } from '@deepseek-ai/dsh-fs'
import { SandboxedFileSystem } from '@deepseek-ai/dsh-fs-sandbox'

export default class TaskFileSystem extends SandboxedFileSystem {
  constructor(ctx, config) {
    super(ctx, config)
    const directory = process.env.AIFN_TASK_DIRECTORY
    if (!directory) throw new Error('aifn: task directory is required')
    this.taskDirectory = realpathSync(directory)
    this.outputDirectory = realpathSync(join(directory, 'output'))
  }

  async mutationPolicy(target, policy) {
    const fresh = await this.resolve(target.displayPath)
    const task = await this.resolve(this.taskDirectory)
    if (!this.contains(task, fresh)) return { target: fresh, policy }
    const output = await this.resolve(this.outputDirectory)
    if (!this.contains(output, fresh)) {
      throw new FsError('Task instructions and submission records are read-only', 'FS_SANDBOX_DENIED')
    }
    if (existsSync(join(this.taskDirectory, 'accepted.json'))) {
      throw new FsError('This task is submitted. Stop editing.', 'FS_SANDBOX_DENIED')
    }
    return {
      target: fresh,
      policy: { ...policy, mode: 'workspace-write', workspaceRoot: this.outputDirectory },
    }
  }

  async writeText(target, content, expected, signal, policy) {
    const allowed = await this.mutationPolicy(target, policy)
    return super.writeText(allowed.target, content, expected, signal, allowed.policy)
  }

  async editText(target, edit, expected, signal, policy) {
    const allowed = await this.mutationPolicy(target, policy)
    return super.editText(allowed.target, edit, expected, signal, allowed.policy)
  }
}
