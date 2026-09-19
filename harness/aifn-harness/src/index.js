import { defineTool } from '@deepseek-ai/dsh-tools'
import { description, parameters, toolName } from './config.js'
import { alreadyAnswered, REASON_DESCRIPTION } from './prompts.js'

/** Stable Cordis plugin name. */
export const name = 'aifn-harness'

/** Services this row waits for; the base bundle provides `tools`. */
export const inject = ['tools']

/**
 * The two tools one AI function call may answer with.
 *
 * This bundle holds the mechanism and nothing about any particular function:
 * how a tool is registered, how a call ends the turn, and how a second answer is
 * refused. What this call is arrives as config, written per call by
 * `aifn.patch` and layered on with `--patch`, because the schema comes from a
 * Pydantic model that exists only on the Python side. Nothing here is derived
 * from a table of functions, so a bundle installed once serves every AI function
 * the caller ever declares.
 *
 * Both tools conclude the turn and both are terminal. A refusal is an answer:
 * it is a conclusion about the input, and a session that keeps going after one
 * can only produce a second, contradicting answer for the same call.
 */
export function apply(ctx, config = {}) {
  const submit = toolName(config, 'submit')
  const refuse = toolName(config, 'refuse')
  if (submit === refuse) {
    throw new Error(`aifn-harness: \`submit\` and \`refuse\` must differ, both are ${submit}`)
  }

  // Answered calls, keyed by the calling agent so one runtime can serve several
  // sessions. This lives and dies with the plugin's fiber, which is the same
  // lifetime as the call: nothing about it needs to survive the session, because
  // the answer the caller reads is the `tool/call` event, not this.
  const answered = new Set()
  const key = exec => exec.agent?.id ?? 'root'

  ctx.tools.register(defineTool({
    name: submit,
    description: description(config, 'submitDescription'),
    parameters: parameters(config),
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          recorded: { type: 'boolean' },
          answer: { type: 'string' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: `Recorded ${value.answer}.` }],
    },
    execute(args, exec) {
      answered.add(key(exec))
      exec.concludeTurn()
      return Promise.resolve({ recorded: true, answer: summarise(args) })
    },
  }))

  ctx.tools.register(defineTool({
    name: refuse,
    description: description(config, 'refuseDescription'),
    parameters: {
      reason: { type: 'string', required: true, description: REASON_DESCRIPTION },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { recorded: { type: 'boolean' } },
      },
      render: () => [{ type: 'text', text: 'Recorded that this input has no answer.' }],
    },
    execute(_args, exec) {
      answered.add(key(exec))
      exec.concludeTurn()
      return Promise.resolve({ recorded: true })
    },
  }))

  // The guard runs before `execute`, so it stops the rest of a response that
  // called twice. `concludeTurn` ends the turn the accepted call was made in,
  // and the calls already in that same response would otherwise still run.
  ctx.tools.guard(exec => answered.has(key(exec)) ? alreadyAnswered(exec.name) : undefined)
}

/**
 * The line the model reads back after answering, describing what it just sent.
 *
 * Counts of the fields that carried lists, because that is the part of an answer
 * a model gets wrong in a way it could notice: submitting one service when it
 * found forty is visible in `1 names`, while an echo of the whole answer would
 * cost the context of the answer twice over.
 */
function summarise(args) {
  const counted = Object.entries(args ?? {})
    .filter(([, value]) => Array.isArray(value))
    .map(([field, value]) => `${value.length} ${field}`)
  const fields = Object.keys(args ?? {}).length
  if (counted.length > 0) return counted.join(', ')
  return fields > 0 ? `${fields} field(s)` : 'an answer with no fields'
}
