import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    projects: [
      'server/apps/auth',
      'server/apps/api',
      'apps/stage-tamagotchi/vitest.node.config.ts',
      'packages/ccc',
      'packages/core-agent',
      'packages/i18n',
      'packages/input-gamepad',
      'packages/input-gamepad-vueuse',
      'packages/model-driver-lipsync',
      'packages/better-ws',
      'packages/plugin-sdk',
      'packages/plugin-sdk-tamagotchi',
      'packages/server-runtime',
      'packages/server-sdk',
      'packages/stage-shared',
      'packages/vitest-plugin-fakemic',
    ],
  },
})
