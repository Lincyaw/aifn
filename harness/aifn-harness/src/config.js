import { isAbsolute } from 'node:path'

export function configuration(config) {
  if (typeof config.directory !== 'string' || !isAbsolute(config.directory)) {
    throw new Error('aifn-harness: directory must be an absolute task directory')
  }
  const endpoint = process.env.AIFN_OUTPUT_ENDPOINT
  const token = process.env.AIFN_OUTPUT_TOKEN
  const validation = config.validation ?? 'pydantic'
  if (!['pydantic', 'json-schema'].includes(validation)) {
    throw new Error('aifn-harness: unknown validation mode')
  }
  if (validation === 'pydantic' && (!endpoint || !token)) {
    throw new Error('aifn-harness: output validation service is required')
  }
  const url = endpoint ? new URL(endpoint) : undefined
  if (validation === 'pydantic' && (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.pathname !== '/output')) {
    throw new Error('aifn-harness: validation service must use a local output endpoint')
  }
  const permission = process.env.DSH_PERMISSION_MODE
  if (!['read-only', 'workspace-write', 'danger-full-access'].includes(permission)) {
    throw new Error('aifn-harness: a valid DSH_PERMISSION_MODE is required')
  }
  if (validation === 'json-schema' && (typeof config.receipt !== 'string' || !isAbsolute(config.receipt))) {
    throw new Error('aifn-harness: a private controller receipt path is required')
  }
  return { directory: config.directory, endpoint, token, permission, validation, receipt: config.receipt }
}
