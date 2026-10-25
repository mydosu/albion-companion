import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '../../..')
const require = createRequire(path.resolve(process.argv[2], 'package.json'))
const esbuild = require(path.resolve(process.argv[2]))
await esbuild.build({
  entryPoints: [path.join(here, 'shell_renderer_harness.ts')], bundle: true,
  format: 'iife', globalName: 'TtsRendererProbe', outfile: process.argv[3],
  plugins: [{name: 'local-auth-adapter', setup(build) {
    build.onResolve({filter: /^\.\.\/(auth|server)$/}, args => ({path: args.path, namespace: 'probe'}))
    build.onLoad({filter: /.*/, namespace: 'probe'}, args => ({contents: args.path.endsWith('auth')
      ? 'export const getAuthToken = () => undefined' : 'export const SERVER_URL = "http://127.0.0.1:9885"'}))
  }}],
})
console.log(`Bundled repository renderer harness from ${root}`)
