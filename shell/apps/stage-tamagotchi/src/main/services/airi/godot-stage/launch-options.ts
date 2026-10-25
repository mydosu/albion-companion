import process from 'node:process'

/** Resolves engine flags before the `--` separator. Headless mode is opt-in. */
export function resolveGodotStageLaunchOptions(env: NodeJS.ProcessEnv = process.env) {
  const headless = ['1', 'on', 'true', 'yes'].includes((env.AIRI_GODOT_STAGE_HEADLESS ?? '').trim().toLowerCase())
  const remoteDebugEnabled = ['1', 'on', 'true', 'yes'].includes((env.GODOT_STAGE_REMOTE_DEBUG ?? '').trim().toLowerCase())
  const remoteDebugUri = remoteDebugEnabled
    ? env.GODOT_STAGE_REMOTE_DEBUG_URI?.trim() || 'tcp://127.0.0.1:6007'
    : undefined
  const engineArgs = headless ? ['--headless'] : []
  if (remoteDebugUri)
    engineArgs.push('--remote-debug', remoteDebugUri)
  return { engineArgs, headless, remoteDebugUri }
}
