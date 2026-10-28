import { useEffect, useState, type ReactNode } from 'react'
import { ApiError } from './api'

/** A small "?" icon that reveals help text on hover or keyboard focus. */
export function Help({ text }: { text: string }) {
  return (
    <span className="help" tabIndex={0} role="img" aria-label={text} data-tip={text}>
      ?
    </span>
  )
}

export function Status({ value }: { value: string }) {
  return <span className={`status ${value}`}>{value}</span>
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof ApiError ? `${error.code}: ${error.message}` : error instanceof Error ? error.message : String(error)
  return <p className="error">{message}</p>
}

export function fmtTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value.endsWith('Z') || value.includes('+') ? value : value + 'Z')
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function fmtBytes(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}

export function fmtSeconds(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toFixed(1)} s`
}

export function useToast(): [ReactNode, (message: string) => void] {
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => setMessage(null), 4000)
    return () => clearTimeout(timer)
  }, [message])
  return [message ? <div className="toast">{message}</div> : null, setMessage]
}

export function Confirm({ label, onConfirm, className = 'danger' }: { label: string; onConfirm: () => void; className?: string }) {
  const [armed, setArmed] = useState(false)
  if (!armed) return <button className={className} onClick={() => setArmed(true)}>{label}</button>
  return (
    <span className="row">
      <button className="danger" onClick={() => { setArmed(false); onConfirm() }}>Confirm {label.toLowerCase()}</button>
      <button onClick={() => setArmed(false)}>Cancel</button>
    </span>
  )
}
