/**
 * The bundle's own checks on the call layer it is configured with.
 *
 * Every field here comes from a file `aifn.patch` wrote moments ago, so a
 * failure means the two sides disagree about the shape of that file. Failing in
 * `apply` makes the disagreement a startup error naming the field, rather than a
 * session that boots with no way to answer and spends a whole model call finding
 * that out.
 */

/** The wire constraint on a tool name: `[A-Za-z0-9_-]`, at most 64 characters. */
const TOOL_NAME = /^[A-Za-z0-9_-]{1,64}$/

/**
 * One tool's name, as the launch layer spells it.
 *
 * The names are carried rather than derived from the function's name here,
 * because `aifn.submit` on the Python side already derives them and the caller
 * reads a transcript by looking for exactly those two strings. A bundle that
 * rebuilt them would be a second place the convention lives, and the two would
 * disagree the first time either changed.
 */
export function toolName(config, field) {
  const name = config[field]
  if (typeof name !== 'string' || !TOOL_NAME.test(name)) {
    throw new Error(
      `aifn-harness: \`${field}\` must be a tool name of 1 to 64 characters from `
      + `[A-Za-z0-9_-], got ${JSON.stringify(name)}`,
    )
  }
  return name
}

/**
 * The submit tool's parameter schema, as the launch layer carries it.
 *
 * A plain object of property names, which is what the parameter DSL calls an
 * implicit open root. It may be empty: an output type with no fields says the
 * answer has no content, and then the call itself is the whole answer.
 */
export function parameters(config) {
  const spec = config.parameters
  if (typeof spec !== 'object' || spec === null || Array.isArray(spec)) {
    throw new Error('aifn-harness: `parameters` must be an object of property schemas')
  }
  return spec
}

/** One of the two tool descriptions the model reads. */
export function description(config, field) {
  const value = config[field]
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`aifn-harness: \`${field}\` must be a non-empty string`)
  }
  return value
}
