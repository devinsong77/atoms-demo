import React, { useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

export default function TerminalConsole({ containerId, theme = 'dark' }) {
  const hostRef = useRef(null)

  useEffect(() => {
    if (!hostRef.current || !containerId) return undefined
    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "'Cascadia Code','SFMono-Regular',Consolas,monospace",
      fontSize: 14,
      lineHeight: 1.28,
      scrollback: 5000,
      theme: theme === 'dark'
        ? { background: '#111016', foreground: '#d8fbe5', cursor: '#8b7cf6', selectionBackground: '#6d5ce866' }
        : { background: '#18171d', foreground: '#d8fbe5', cursor: '#9d90ff' },
    })
    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(hostRef.current)
    fit.fit()
    terminal.write('\x1b[90mConnecting to Docker exec shell…\x1b[0m\r\n')
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const token = localStorage.getItem('atoms_token') || ''
    const socket = new WebSocket(`${protocol}//${location.host}/api/cloud/containers/${containerId}/terminal?token=${encodeURIComponent(token)}`)
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => terminal.focus()
    socket.onmessage = async event => {
      const data = event.data instanceof Blob ? await event.data.arrayBuffer() : event.data
      terminal.write(typeof data === 'string' ? data : new Uint8Array(data))
    }
    socket.onerror = () => terminal.write('\r\n\x1b[31mTerminal connection failed.\x1b[0m\r\n')
    socket.onclose = () => terminal.write('\r\n\x1b[90m[session closed]\x1b[0m\r\n')
    const input = terminal.onData(data => { if (socket.readyState === WebSocket.OPEN) socket.send(data) })
    const resize = () => fit.fit()
    const observer = new ResizeObserver(resize)
    observer.observe(hostRef.current)
    return () => {
      observer.disconnect()
      input.dispose()
      socket.close()
      terminal.dispose()
    }
  }, [containerId, theme])

  return <div className="xterm-host" ref={hostRef}/>
}
