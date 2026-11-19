export type Point = { x: number; y: number }
export type Size = { width: number; height: number }
export type AssistantLayout = { version: 1; position: Point; size: Size }
export type Viewport = Size
export const LAUNCHER_SIZE = 48
export const PANEL_BOTTOM = 84
export const MOBILE_WIDTH = 600
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max))

export function fitPosition(position: Point, viewport: Viewport): Point {
  return { x: clamp(position.x, 8, viewport.width - LAUNCHER_SIZE - 8), y: clamp(position.y, 8, viewport.height - LAUNCHER_SIZE - 8) }
}
export function fitSize(size: Size, viewport: Viewport): Size {
  if (viewport.width <= MOBILE_WIDTH) return { width: Math.max(1, viewport.width - 24), height: Math.max(1, viewport.height - 24) }
  if (viewport.height <= 520) return { width: clamp(size.width, Math.min(360, viewport.width - 24), Math.min(720, viewport.width - 24)), height: Math.max(1, viewport.height - 24) }
  const maxWidth = Math.min(720, viewport.width - 40)
  const maxHeight = Math.max(1, viewport.height - PANEL_BOTTOM - 20)
  return { width: clamp(size.width, Math.min(360, maxWidth), maxWidth), height: clamp(size.height, Math.min(420, maxHeight), maxHeight) }
}
export function defaultLayout(viewport: Viewport): AssistantLayout {
  return { version: 1, position: fitPosition({ x: viewport.width - 68, y: viewport.height - 68 }, viewport), size: { width: 440, height: 680 } }
}
export function loadLayout(raw: string | null, viewport: Viewport): AssistantLayout {
  try {
    const value = JSON.parse(raw ?? 'null')
    if (value?.version === 1 && [value.position?.x, value.position?.y, value.size?.width, value.size?.height]
      .every((number) => typeof number === 'number' && Number.isFinite(number))) {
      return { version: 1, position: fitPosition(value.position, viewport), size: { width: value.size.width, height: value.size.height } }
    }
  } catch { /* Invalid preferences fall back to the default placement. */ }
  return defaultLayout(viewport)
}
