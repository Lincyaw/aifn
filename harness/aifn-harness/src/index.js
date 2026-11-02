import { defineTool } from '@deepseek-ai/dsh-tools'
import { configuration } from './config.js'
import { alreadyAnswered, descriptions } from './prompts.js'
import { schemaValidator } from './schema.js'

export const name = 'aifn-harness'
export const inject = ['tools']

export function apply(ctx, config = {}) {
  const { endpoint, token, permission, directory, validation, receipt } = configuration(config)
  const local = validation === 'json-schema' ? schemaValidator(directory, receipt) : undefined
  let answered = local?.answered ?? false

  ctx.inject(['permissionPresets'], scope => {
    scope.on('agent/created', ({ agent }) => {
      scope.permissionPresets.set(agent.session, permission)
    })
  })

  for (const [action, tool] of [
    ['validate', 'validate_output'],
    ['submit', 'submit_output'],
    ['refuse', 'refuse_output'],
  ]) {
    const parameters = { path: { type: 'string', required: true, description: 'Candidate file path.' } }
    ctx.tools.register(defineTool({
      name: tool,
      description: local ? descriptions[action].replace('full declared output model and business validators', 'declared JSON Schema') : descriptions[action],
      parameters,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      async execute(args, exec) {
        exec.signal?.throwIfAborted()
        let result
        if (local) result = local.dispatch(action, args.path)
        else {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify({ action, ...args }),
          signal: exec.signal,
        })
        if (!response.ok) {
          throw new Error(`Output validation service failed (${response.status})`)
        }
        result = await response.json()
        }
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
