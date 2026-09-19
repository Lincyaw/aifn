import { defineTool } from '@deepseek-ai/dsh-tools'
import { configuration } from './config.js'
import { alreadyAnswered, descriptions } from './prompts.js'

export const name = 'aifn-harness'
export const inject = ['tools']

export function apply(ctx, config = {}) {
  const { endpoint, token } = configuration(config)
  let answered = false

  for (const [action, tool] of [
    ['validate', 'validate_output'],
    ['submit', 'submit_output'],
    ['refuse', 'refuse_output'],
  ]) {
    const parameters = { path: { type: 'string', required: true, description: 'Candidate file path.' } }
    ctx.tools.register(defineTool({
      name: tool,
      description: descriptions[action],
      parameters,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      async execute(args, exec) {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify({ action, ...args }),
          signal: exec.signal,
        })
        if (!response.ok) {
          throw new Error(`Output validation service failed (${response.status})`)
        }
        const result = await response.json()
        if (result.submitted === true) {
          answered = true
          exec.concludeTurn()
        }
        return result
      },
    }))
  }
  ctx.tools.guard(() => answered ? alreadyAnswered : undefined)
}
