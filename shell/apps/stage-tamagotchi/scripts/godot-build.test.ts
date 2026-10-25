/* eslint-disable no-template-curly-in-string */

import { dirname, resolve } from 'node:path'

import { Packager } from 'electron-builder'
import { describe, expect, it } from 'vitest'

import config from '../electron-builder.config'

// eslint-disable-next-line no-restricted-syntax -- The build hook imports this same Node ESM helper.
import { buildGodotSidecar } from '../../../../tools/godot-sidecar.mjs'

describe('godot packaging matrix', () => {
  it('passes electron-builder configuration validation without packaging', async () => {
    const packager = new Packager({ projectDir: resolve(import.meta.dirname, '..'), config: { ...config, extends: null } })
    await expect(packager.validateConfig()).resolves.toBeUndefined()
  })

  it('pairs each shell architecture with its own sidecar directory', async () => {
    const projectDir = resolve(import.meta.dirname, '..')
    const source = config.extraResources[0].from
    for (const [platform, os, architecture, arch] of [
      ['win32', 'win', 'x64', 1],
      ['darwin', 'mac', 'x64', 1],
      ['darwin', 'mac', 'arm64', 3],
      ['linux', 'linux', 'x64', 1],
      ['linux', 'linux', 'arm64', 3],
    ] as const) {
      const plan = await buildGodotSidecar({ projectDir, platform, arch, dryRun: true })
      const packagedSource = source.replace('${os}', os).replace('${arch}', architecture)
      expect(resolve(projectDir, packagedSource)).toBe(dirname(plan.output))
    }
  })

  it('declares both supported architectures for macOS and Linux packages', () => {
    for (const target of [...config.mac.target, ...config.linux.target])
      expect(target.arch).toEqual(['x64', 'arm64'])
  })
})
