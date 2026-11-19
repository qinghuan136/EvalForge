import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'
import { defaultLayout, fitPosition, fitSize, loadLayout, MOBILE_WIDTH } from './layout'
import type { AssistantLayout } from './layout'

const viewportSize = () => ({ width: window.innerWidth, height: window.innerHeight })
type Gesture = { kind: 'move' | 'resize'; pointerId: number; x: number; y: number; start: AssistantLayout; moved: boolean }

export function useAssistantLayout(userId: number) {
  const key = `evalforge.assistant.layout.v1:${userId}`
  const [viewport, setViewport] = useState(viewportSize)
  const [layout, setLayout] = useState(() => {
    try { return loadLayout(localStorage.getItem(key), viewportSize()) }
    catch { return defaultLayout(viewportSize()) }
  })
  const current = useRef(layout)
  const gesture = useRef<Gesture | undefined>(undefined)
  const dragged = useRef(false)
  function update(next: AssistantLayout) { current.current = next; setLayout(next) }
  function persist() {
    try { localStorage.setItem(key, JSON.stringify(current.current)) }
    catch { /* Geometry remains usable in memory when storage is unavailable. */ }
  }
  useEffect(() => {
    function resize() {
      const nextViewport = viewportSize()
      setViewport(nextViewport)
      const next = { ...current.current, position: fitPosition(current.current.position, nextViewport) }
      current.current = next
      setLayout(next)
    }
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])

  function begin(event: PointerEvent<HTMLElement>, kind: Gesture['kind']) {
    if (event.button !== 0 || (kind === 'resize' && viewport.width <= MOBILE_WIDTH)) return
    dragged.current = false
    gesture.current = { kind, pointerId: event.pointerId, x: event.clientX, y: event.clientY, start: current.current, moved: false }
    event.currentTarget.setPointerCapture(event.pointerId)
    if (kind === 'resize') event.preventDefault()
  }
  function move(event: PointerEvent<HTMLElement>) {
    const active = gesture.current
    if (!active || active.pointerId !== event.pointerId) return
    const dx = event.clientX - active.x, dy = event.clientY - active.y
    if (!active.moved && Math.hypot(dx, dy) < 5) return
    active.moved = true
    dragged.current = true
    update(active.kind === 'move'
      ? { ...current.current, position: fitPosition({ x: active.start.position.x + dx, y: active.start.position.y + dy }, viewportSize()) }
      : { ...current.current, size: fitSize({ width: active.start.size.width - dx, height: active.start.size.height - dy }, viewportSize()) })
  }
  function end(event: PointerEvent<HTMLElement>) {
    if (gesture.current?.pointerId !== event.pointerId) return
    gesture.current = undefined
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    persist() // One write per gesture, rather than synchronous storage writes on every frame.
  }
  function keyboard(event: KeyboardEvent<HTMLElement>, kind: Gesture['kind']) {
    const offsets: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
    const offset = offsets[event.key]
    if (!offset) return
    event.preventDefault()
    const step = event.shiftKey ? 50 : 10
    const dx = offset[0] * step, dy = offset[1] * step
    update(kind === 'move'
      ? { ...current.current, position: fitPosition({ x: current.current.position.x + dx, y: current.current.position.y + dy }, viewport) }
      : { ...current.current, size: fitSize({ width: current.current.size.width - dx, height: current.current.size.height - dy }, viewport) })
    persist()
  }
  const shared = { onPointerMove: move, onPointerUp: end, onPointerCancel: end, onLostPointerCapture: end }
  return {
    position: layout.position, size: fitSize(layout.size, viewport),
    dragHandlers: { ...shared, onPointerDown: (event: PointerEvent<HTMLElement>) => begin(event, 'move'),
      onKeyDown: (event: KeyboardEvent<HTMLElement>) => keyboard(event, 'move') },
    resizeHandlers: { ...shared, onPointerDown: (event: PointerEvent<HTMLElement>) => begin(event, 'resize'),
      onKeyDown: (event: KeyboardEvent<HTMLElement>) => keyboard(event, 'resize') },
    consumeClick: () => { const moved = dragged.current; dragged.current = false; return !moved },
    reset: () => { update(defaultLayout(viewport)); persist() },
  }
}

export type AssistantLayoutController = ReturnType<typeof useAssistantLayout>
