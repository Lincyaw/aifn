import { isAbsolute } from 'node:path'

export function configuration(config) {
  if (typeof config.directory !== 'string' || !isAbsolute(config.directory)) {
    throw new Error('aifn-harness: directory must be an absolute task directory')
  }
  const endpoint = process.env.AIFN_OUTPUT_ENDPOINT
  const token = process.env.AIFN_OUTPUT_TOKEN
  if (!endpoint || !token) {
    throw new Error('aifn-harness: output validation service is required')
  }
  const url = new URL(endpoint)
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.pathname !== '/output') {
    throw new Error('aifn-harness: validation service must use a local output endpoint')
  }
  return { directory: config.directory, endpoint, token }
}
