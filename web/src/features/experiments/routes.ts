export const configListPath = '/experiments'
export const runListPath = '/experiment-runs'
export const runDetailPattern = `${runListPath}/:experimentId`
export const legacyRunDetailPattern = '/experiments/:experimentId'

export function runDetailPath(id: number | string) {
  return `${runListPath}/${encodeURIComponent(id)}`
}

export function configPath(group: string, listSearch = '') {
  const params = new URLSearchParams(listSearch)
  params.set('group', group)
  return `${configListPath}?${params}`
}

export function runsForGroup(group: string) {
  return `${runListPath}?${new URLSearchParams({ group })}`
}

export function runListReturnTo(state: unknown) {
  const destination = (state as { returnTo?: unknown } | null)?.returnTo
  if (typeof destination !== 'string') return runListPath
  if (destination === runListPath || destination.startsWith(runListPath + '?')) return destination
  if (destination.startsWith(configListPath + '?')) {
    const params = new URLSearchParams(destination.slice(configListPath.length))
    if (params.has('group')) {
      params.delete('dataset')
      return `${runListPath}?${params}`
    }
  }
  return runListPath
}

export function positiveId(value: string | null) {
  if (!value || !/^[1-9]\d*$/.test(value) || Number(value) > 2147483647) return undefined
  return Number(value)
}
