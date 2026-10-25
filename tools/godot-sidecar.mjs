import { spawn } from 'node:child_process'
import { access, copyFile, mkdir, readdir, readFile, stat } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

const skyAssetPath = 'packages/stage-ui-three/src/components/Environment/assets/sky_linekotsi_23_HDRI.hdr'
const packagedSkyPath = 'assets/sky_linekotsi_23_HDRI.hdr'

export function resolveGodotExport(projectDir, platform, arch) {
  const engineDir = resolve(projectDir, '../../engines/stage-tamagotchi-godot')
  if (platform === 'win32' && arch === 1) {
    return { engineDir, preset: 'Windows Desktop', output: join(engineDir, 'out/win/x64/godot-stage.exe') }
  }
  if (platform === 'darwin' && [1, 3].includes(arch)) {
    return { engineDir, preset: 'macOS', output: join(engineDir, 'out/mac', arch === 1 ? 'x64' : 'arm64', 'godot-stage.app') }
  }
  if (platform === 'linux' && [1, 3].includes(arch)) {
    return { engineDir, preset: arch === 1 ? 'Linux x64' : 'Linux arm64', output: join(engineDir, 'out/linux', arch === 1 ? 'x64' : 'arm64', 'godot-stage') }
  }
  throw new Error(`No Godot export preset for ${platform}, electron-builder architecture ${arch}.`)
}

export async function validateGodotExport(directory, platform) {
  const binary = platform === 'win32'
    ? 'godot-stage.exe'
    : platform === 'darwin' ? 'godot-stage.app/Contents/MacOS/godot-stage' : 'godot-stage'
  const executable = join(directory, binary)
  if (!(await stat(executable)).isFile())
    throw new Error(`Godot sidecar executable is not a file: ${executable}`)

  // C# exports need the complete data directory, including the .NET runtime.
  const files = await readdir(directory, { recursive: true })
  const runtimeLibrary = platform === 'win32' ? 'hostfxr.dll' : platform === 'darwin' ? 'libhostfxr.dylib' : 'libhostfxr.so'
  for (const required of ['stage-tamagotchi-godot.dll', 'stage-tamagotchi-godot.runtimeconfig.json', runtimeLibrary]) {
    if (!files.some(file => file.replaceAll('\\', '/').split('/').at(-1) === required))
      throw new Error(`Godot C# export is incomplete. Missing ${required} under ${directory}.`)
  }
  const resourceRoot = platform === 'darwin' ? join(directory, 'godot-stage.app/Contents/Resources') : directory
  await access(join(resourceRoot, packagedSkyPath))
  return executable
}

function runGodot(executable, args, cwd) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(executable, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true })
    let reportedError = false
    for (const [stream, destination] of [[child.stdout, process.stdout], [child.stderr, process.stderr]]) {
      let pending = ''
      stream.on('data', (chunk) => {
        destination.write(chunk)
        pending += chunk.toString()
        const lines = pending.split(/\r?\n/)
        pending = lines.pop()
        if (lines.some(line => /^(?:SCRIPT ERROR:|ERROR:)/.test(line)))
          reportedError = true
      })
      stream.on('end', () => {
        if (/^(?:SCRIPT ERROR:|ERROR:)/.test(pending))
          reportedError = true
      })
    }
    child.once('error', () => reject(new Error('Cannot start Godot. Set GODOT4 to the Godot 4.7.1 .NET executable, or install it on PATH.')))
    child.once('close', (code, signal) => {
      if (code === 0 && !reportedError)
        resolveRun()
      else
        reject(new Error(`Godot export failed (exit ${code}, signal ${signal}, script errors ${reportedError}).`))
    })
  })
}

// Dry runs validate the same preset, source assets, and commands used by beforePack.
export async function buildGodotSidecar({ projectDir, platform, arch, dryRun = false }) {
  const { engineDir, preset, output } = resolveGodotExport(projectDir, platform, arch)
  await access(join(engineDir, 'project.godot'))
  const presets = await readFile(join(engineDir, 'export_presets.cfg'), 'utf8')
  if (!presets.includes(`name="${preset}"`))
    throw new Error(`Godot export preset is missing: ${preset}`)
  const skySource = resolve(projectDir, '../..', skyAssetPath)
  await access(skySource)
  const executable = process.env.GODOT4?.trim() || 'godot'
  const commands = [
    ['--headless', '--path', engineDir, '--editor', '--import'],
    ['--headless', '--path', engineDir, '--export-release', preset, output],
  ]
  const resourceRoot = platform === 'darwin' ? join(output, 'Contents/Resources') : dirname(output)
  const skyOutput = join(resourceRoot, packagedSkyPath)
  const plan = { platform, arch, preset, output, executable, commands, skySource, skyOutput }
  if (dryRun)
    return plan
  await mkdir(dirname(output), { recursive: true })
  for (const args of commands)
    await runGodot(executable, args, engineDir)
  await mkdir(dirname(skyOutput), { recursive: true })
  await copyFile(skySource, skyOutput)
  await validateGodotExport(dirname(output), platform)
  return plan
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { values } = parseArgs({ options: {
    platform: { type: 'string', default: process.platform },
    arch: { type: 'string', default: process.arch },
    'dry-run': { type: 'boolean', default: false },
  } })
  const projectDir = resolve(dirname(fileURLToPath(import.meta.url)), '../shell/apps/stage-tamagotchi')
  try {
    const plan = await buildGodotSidecar({ projectDir, platform: values.platform, arch: { x64: 1, arm64: 3 }[values.arch], dryRun: values['dry-run'] })
    console.log(JSON.stringify(plan, null, 2))
  }
  catch (error) {
    console.error(error.message)
    process.exitCode = 1
  }
}
