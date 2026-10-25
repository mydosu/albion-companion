import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { buildGodotSidecar, resolveGodotExport, validateGodotExport } from '../../tools/godot-sidecar.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const projectDir = join(root, 'shell/apps/stage-tamagotchi')

test('selects the export preset for the target, independent of cwd', () => {
  assert.equal(resolveGodotExport(projectDir, 'win32', 1).preset, 'Windows Desktop')
  assert.equal(resolveGodotExport(projectDir, 'linux', 3).preset, 'Linux arm64')
  assert.equal(resolveGodotExport(projectDir, 'linux', 1).preset, 'Linux x64')
  assert.equal(resolveGodotExport(projectDir, 'darwin', 3).preset, 'macOS')
  assert.notEqual(resolveGodotExport(projectDir, 'linux', 1).output, resolveGodotExport(projectDir, 'linux', 3).output)
  assert.notEqual(resolveGodotExport(projectDir, 'darwin', 1).output, resolveGodotExport(projectDir, 'darwin', 3).output)
  assert.throws(() => resolveGodotExport(projectDir, 'darwin', 0), /No Godot export preset/)
  assert.throws(() => resolveGodotExport(projectDir, 'win32', 3), /No Godot export preset/)
})

test('rejects missing executables and incomplete C# data, then accepts the complete export', async () => {
  const output = join(root, 'tests/output/shell-s3')
  await mkdir(output, { recursive: true })
  const directory = await mkdtemp(join(output, 'export-check-'))
  try {
    await assert.rejects(validateGodotExport(directory, 'win32'), /ENOENT/)
    await mkdir(join(directory, 'godot-stage.exe'))
    await assert.rejects(validateGodotExport(directory, 'win32'), /not a file/)
    await rm(join(directory, 'godot-stage.exe'), { recursive: true })
    await writeFile(join(directory, 'godot-stage.exe'), 'test executable')
    await assert.rejects(validateGodotExport(directory, 'win32'), /Missing stage-tamagotchi-godot.dll/)
    const data = join(directory, 'data/godot')
    await mkdir(data, { recursive: true })
    await writeFile(join(data, 'stage-tamagotchi-godot.dll'), 'test assembly')
    await assert.rejects(validateGodotExport(directory, 'win32'), /Missing stage-tamagotchi-godot.runtimeconfig.json/)
    await writeFile(join(data, 'stage-tamagotchi-godot.runtimeconfig.json'), '{}')
    await assert.rejects(validateGodotExport(directory, 'win32'), /Missing hostfxr.dll/)
    await writeFile(join(data, 'hostfxr.dll'), 'test runtime')
    await assert.rejects(validateGodotExport(directory, 'win32'), /ENOENT/)
    const sky = join(directory, 'assets/sky_linekotsi_23_HDRI.hdr')
    await mkdir(dirname(sky), { recursive: true })
    await writeFile(sky, 'test sky')
    assert.equal(await validateGodotExport(directory, 'win32'), join(directory, 'godot-stage.exe'))
  }
  finally {
    await rm(directory, { recursive: true, force: true })
  }
})

for (const [platform, executable, runtime] of [
  ['darwin', 'godot-stage.app/Contents/MacOS/godot-stage', 'libhostfxr.dylib'],
  ['linux', 'godot-stage', 'libhostfxr.so'],
]) {
  test(`validates the ${platform} bundle and its own HDR resource root`, async () => {
    const output = join(root, 'tests/output/shell-s3')
    await mkdir(output, { recursive: true })
    const directory = await mkdtemp(join(output, 'export-check-'))
    try {
      const binary = join(directory, executable)
      await mkdir(dirname(binary), { recursive: true })
      await writeFile(binary, 'test executable')
      const resourceRoot = platform === 'darwin' ? join(directory, 'godot-stage.app/Contents/Resources') : directory
      await mkdir(join(resourceRoot, 'data'), { recursive: true })
      for (const name of ['stage-tamagotchi-godot.dll', 'stage-tamagotchi-godot.runtimeconfig.json', runtime])
        await writeFile(join(resourceRoot, 'data', name), 'test runtime')
      await assert.rejects(validateGodotExport(directory, platform), /ENOENT/)
      await mkdir(join(resourceRoot, 'assets'))
      await writeFile(join(resourceRoot, 'assets/sky_linekotsi_23_HDRI.hdr'), 'test sky')
      assert.equal(await validateGodotExport(directory, platform), binary)
    }
    finally {
      await rm(directory, { recursive: true, force: true })
    }
  })
}

test('dry-runs all supported desktop exports without invoking Godot', async () => {
  for (const [platform, arch, os, architecture] of [
    ['win32', 1, 'win', 'x64'], ['darwin', 1, 'mac', 'x64'], ['darwin', 3, 'mac', 'arm64'],
    ['linux', 1, 'linux', 'x64'], ['linux', 3, 'linux', 'arm64'],
  ]) {
    const plan = await buildGodotSidecar({ projectDir, platform, arch, dryRun: true })
    assert.equal(dirname(plan.output), resolve(projectDir, '../../engines/stage-tamagotchi-godot/out', os, architecture))
    assert.deepEqual(plan.commands[1].slice(-3), ['--export-release', plan.preset, plan.output])
    assert.equal(plan.commands.every(args => args[0] === '--headless'), true)
    assert.equal(plan.skyOutput.includes('packages'), false)
  }
})
