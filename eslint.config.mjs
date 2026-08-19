export default [
  {
    files: ['main.js', 'config.js', 'config.js.example'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        window: 'readonly',
        document: 'readonly',
        fetch: 'readonly',
        Image: 'readonly',
        google: 'readonly',
        performance: 'readonly',
        requestAnimationFrame: 'readonly',
        setTimeout: 'readonly',
        setInterval: 'readonly',
        clearInterval: 'readonly',
        Float32Array: 'readonly',
        navigator: 'readonly',
        location: 'readonly',
        URLSearchParams: 'readonly',
      },
    },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': ['error', { caughtErrors: 'none' }],
      'no-redeclare': 'error',
      'no-dupe-keys': 'error',
      'no-dupe-args': 'error',
      'no-unreachable': 'error',
      'no-constant-condition': 'error',
      'no-self-assign': 'error',
      // обработчики событий зовутся после инициализации модуля — лексический
      // порядок объявлений let/const в них не важен
      'no-use-before-define': ['error', { functions: false, variables: false }],
      'no-var': 'error',
      'prefer-const': 'error',
    },
  },
  { ignores: ['vendor/'] },
];
