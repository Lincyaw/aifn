import { createHash, randomUUID } from 'node:crypto'
import { constants, existsSync, fstatSync, openSync, closeSync, readSync, realpathSync, renameSync, writeFileSync, unlinkSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

const MAX_BYTES = 16 * 1024 * 1024
const error = (code, message, extra = {}) => ({ valid: false, errors: [{ code, message, ...extra }] })

function readText(filename) {
  const fd = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    if (!fstatSync(fd).isFile()) throw new Error('Expected a regular file')
    const buffer = Buffer.alloc(MAX_BYTES + 1)
    let size = 0, count
    while (size < buffer.length && (count = readSync(fd, buffer, size, buffer.length - size, null))) size += count
    if (size > MAX_BYTES) throw new Error('Output/schema exceeds 16 MiB')
    return new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, size))
  } finally { closeSync(fd) }
}

export function schemaValidator(directory, receipt) {
  const root = realpathSync(directory)
  const output = realpathSync(join(root, 'output'))
  const ajv = new Ajv2020({ allErrors: true, strict: false, coerceTypes: false, useDefaults: true, ownProperties: true })
  addFormats(ajv)
  const validators = Object.fromEntries(['output', 'refusal'].map(kind => {
    const schema = readText(join(root, `${kind}.schema.json`))
    return [kind, { validate: ajv.compile(JSON.parse(schema)), sha256: createHash('sha256').update(schema).digest('hex') }]
  }))
  let cached
  return {
    get answered() { return existsSync(receipt) },
    dispatch(action, candidate) {
      if (existsSync(receipt)) return error('already_submitted', 'This task is submitted. Stop editing.')
      const filename = isAbsolute(candidate) ? candidate : join(output, candidate)
      const kind = resolve(filename) === join(output, 'refusal.json') ? 'refusal' : 'output'
      if (!['output.json', 'refusal.json'].some(name => resolve(filename) === join(output, name))
          || action === 'submit' && kind !== 'output' || action === 'refuse' && kind !== 'refusal') {
        return error('output_path', 'Use this task\'s designated output files only.')
      }
      let content, value
      try {
        if (realpathSync(filename) !== resolve(filename)) return error('output_path', 'Symbolic links are not accepted.')
        content = readText(filename)
      } catch (caught) {
        if (caught.code === 'ENOENT') return error('file_missing', 'Write the output file first.')
        return error('file_read', caught.message)
      }
      const hash = createHash('sha256').update(kind).update(content).digest('hex')
      if (cached?.hash === hash) ({ value } = cached)
      else {
        try { value = JSON.parse(content) }
        catch (caught) { return error('json_syntax', caught.message) }
        const unsafeNumber = item => typeof item === 'number' ? !Number.isFinite(item) || Number.isInteger(item) && !Number.isSafeInteger(item)
          : item && typeof item === 'object' ? Object.values(item).some(unsafeNumber) : false
        if (unsafeNumber(value)) return error('number_range', 'Use finite numbers and integers within JavaScript safe range.')
        const { validate } = validators[kind]
        const valid = validate(value)
        if (unsafeNumber(value)) return error('number_range', 'Defaults must use finite numbers and safe integers.')
        const report = valid ? { valid: true, errors: [] } : { valid: false, errors: validate.errors.map(item => ({
          path: item.instancePath, code: item.keyword, message: item.message, params: item.params,
        })) }
        cached = { hash, value, report }
      }
      if (!cached.report.valid || action === 'validate') return cached.report
      const temporary = join(dirname(receipt), `.submission-${randomUUID()}.tmp`)
      try {
        writeFileSync(temporary, JSON.stringify({ status: kind === 'refusal' ? 'refused' : 'returned',
          value, submitted_json: JSON.stringify(value), validation: 'json-schema', schema_sha256: validators[kind].sha256 }), { flag: 'wx', mode: 0o400 })
        renameSync(temporary, receipt)
      } finally { if (existsSync(temporary)) unlinkSync(temporary) }
      return { valid: true, submitted: true }
    },
  }
}
