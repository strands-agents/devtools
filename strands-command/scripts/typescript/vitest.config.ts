import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    // Several test files create/delete the same .artifact JSONL path; parallel
    // file execution would race on it.
    fileParallelism: false,
  },
})
