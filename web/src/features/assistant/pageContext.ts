import { matchPath } from 'react-router'
import { legacyRunDetailPattern, runDetailPattern } from '../experiments/routes'

export type PageContext =
  | { type: 'experiment'; id: number }
  | { type: 'job'; experiment_id: number; job_id: string }
  | { type: 'dataset'; id: number; version_id?: number | null }
  | { type: 'runner'; id: string }

export function derivePageContext(pathname: string): PageContext | undefined {
  const id = (matchPath(runDetailPattern, pathname) ?? matchPath(legacyRunDetailPattern, pathname))?.params.experimentId
  if (!id || !/^[1-9]\d*$/.test(id) || Number(id) > 2147483647) return undefined
  return { type: 'experiment', id: Number(id) }
}

export function pageContextLabel(context: PageContext): string {
  switch (context.type) {
    case 'experiment': return `实验 #${context.id}`
    case 'job': return `实验 #${context.experiment_id} 的 Job ${context.job_id}`
    case 'dataset': return context.version_id == null
      ? `数据集 #${context.id}` : `数据集 #${context.id} · 版本 #${context.version_id}`
    case 'runner': return `Runner ${context.id}`
  }
}
