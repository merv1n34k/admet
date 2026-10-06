import { defineConfig } from 'vitepress'

// GitHub Pages serves the site under /<repository>/; locally it is served at /.
const base = process.env.DOCS_BASE ?? '/'

export default defineConfig({
  base,
  title: 'ADMET',
  description: 'Microfluidics acquisition and analysis: drive the rig, record runs, analyse projects.',
  cleanUrls: true,
  lastUpdated: true,
  head: [['link', { rel: 'icon', href: `${base}logo.png` }]],
  themeConfig: {
    logo: '/logo.png',
    nav: [
      { text: 'Guide', link: '/guide/introduction' },
      { text: 'Control', link: '/control/overview' },
      { text: 'Analyze', link: '/analyze/overview' },
      { text: 'Protocols', link: '/protocols/format' },
      { text: 'Reference', link: '/reference/cli' },
    ],
    sidebar: [
      {
        text: 'Guide',
        items: [
          { text: 'Introduction', link: '/guide/introduction' },
          { text: 'Installation', link: '/guide/installation' },
          { text: 'Projects', link: '/guide/projects' },
        ],
      },
      {
        text: 'admet control',
        items: [
          { text: 'Overview', link: '/control/overview' },
          { text: 'Calibration', link: '/control/calibration' },
          { text: 'Running protocols', link: '/control/running' },
          { text: 'Manual control', link: '/control/manual' },
          { text: 'Calculations', link: '/control/calculations' },
          { text: 'Safety', link: '/control/safety' },
        ],
      },
      {
        text: 'admet analyze',
        items: [{ text: 'Overview', link: '/analyze/overview' }],
      },
      {
        text: 'Protocols',
        items: [
          { text: 'Protocol format', link: '/protocols/format' },
          { text: 'Parameters and units', link: '/protocols/parameters' },
          { text: 'Bundled templates', link: '/protocols/templates' },
        ],
      },
      {
        text: 'Calculations',
        items: [
          { text: 'Fluid density', link: '/calculations/density' },
          { text: 'Gravimetry', link: '/calculations/gravimetry' },
          { text: 'Dead volume', link: '/calculations/dead-volume' },
          { text: 'Viscosity', link: '/calculations/viscosity' },
          { text: 'Flow scout and recording summary', link: '/calculations/other' },
        ],
      },
      {
        text: 'Reference',
        items: [
          { text: 'Command line', link: '/reference/cli' },
          { text: 'Python API', link: '/reference/python-api' },
          { text: 'Development', link: '/reference/development' },
        ],
      },
    ],
    socialLinks: [{ icon: 'github', link: 'https://github.com/merv1n34k/admet' }],
    search: { provider: 'local' },
    footer: { message: 'Released under the GNU AGPL v3.0.' },
    outline: [2, 3],
  },
})
