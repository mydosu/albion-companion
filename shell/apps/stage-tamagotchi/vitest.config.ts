import { resolve } from 'node:path'
import { cwd } from 'node:process'

import vue from '@vitejs/plugin-vue'
import UnoCss from 'unocss/vite'
import Info from 'unplugin-info/vite'

import { playwright } from '@vitest/browser-playwright'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  root: import.meta.dirname,
  // Some Windows hosts block IPv6 loopback even when localhost resolves to ::1.
  server: { host: '127.0.0.1' },
  resolve: { dedupe: ['vue'] },
  optimizeDeps: { entries: ['src/**/*.browser.test.ts'] },
  plugins: [
    Info(),
    vue(),
    UnoCss(),
  ],
  test: {
    env: loadEnv('test', cwd(), ''),
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          include: ['src/**/*.test.ts', 'scripts/**/*.test.ts'],
          exclude: ['src/**/*.browser.test.ts', '**/node_modules/**', '**/.git/**'],
          fileParallelism: false,
          maxWorkers: 1,
        },
      },
      {
        extends: true,
        test: {
          name: 'browser',
          include: ['src/**/*.browser.test.ts'],
          exclude: ['**/node_modules/**', '**/.git/**'],
          browser: {
            enabled: true,
            headless: true,
            screenshotDirectory: resolve(import.meta.dirname, '../../../tests/output/shell-s3/screenshots/desktop'),
            provider: playwright(),
            instances: [
              { browser: 'chromium' },
            ],
          },
        },
      },
    ],
  },
})
