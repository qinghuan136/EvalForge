const timeFormatter = new Intl.DateTimeFormat(undefined, {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
  minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hourCycle: 'h23',
})
const durationFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 })

export function displayTime(nanos: string) {
  return timeFormatter.format(new Date(Number(BigInt(nanos) / 1_000_000n)))
}

export function displayDuration(start: string, end: string) {
  return `${durationFormatter.format(Number(BigInt(end) - BigInt(start)) / 1_000_000)} ms`
}

export function statusLabel(code: number | null) {
  if (code === null || code === 0) return '未设置'
  if (code === 1) return '正常'
  if (code === 2) return '错误'
  return `状态 ${code}`
}

export function jobReturnTo(state: unknown): string | undefined {
  const destination = (state as { jobReturnTo?: unknown } | null)?.jobReturnTo
  return typeof destination === 'string' && /^\/(?:experiments|experiment-runs)\/[1-9]\d*\?/.test(destination)
    && new URLSearchParams(destination.split('?')[1]).has('job') ? destination : undefined
}
