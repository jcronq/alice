import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'

// Slice-1 entry point. Mount <App /> into #root; <App /> owns the
// AG-UI SSE subscription and renders <RunningNowPanel />.
const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('status-site react: #root element missing')
ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
