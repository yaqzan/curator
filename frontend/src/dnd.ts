import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'

/** Pixels of travel before a press becomes a drag rather than a click. */
const THRESHOLD = 5

export type DragState<P, T> = {
  payload: P
  /** Viewport coordinates of the pointer, for positioning the drag ghost. */
  x: number
  y: number
  target: T | null
}

/**
 * A pointer-driven drag with no library and no HTML5 drag-and-drop.
 *
 * Native `dragstart`/`dragover` gives no control over the drag image and cannot
 * express "drop *onto* this row means tie, drop *between* rows means rank" —
 * both need the live pointer position against a row's own rectangle. So the drop
 * target is resolved by the caller from raw coordinates via `resolve`.
 *
 * **Mouse and pen only, on purpose.** A touch drag has to claim the gesture from
 * the browser's scroller, which only `touch-action: none` reliably does — and
 * that would kill scrolling on the very elements filling a phone screen.
 * Tap-to-place already covers touch and needs nothing from this hook.
 */
export function usePointerDrag<P, T>(opts: {
  resolve: (x: number, y: number, payload: P) => T | null
  onDrop: (payload: P, target: T | null) => void
}) {
  const [drag, setDrag] = useState<DragState<P, T> | null>(null)

  // The callbacks are re-created on every render; the window listeners are not.
  const latest = useRef(opts)
  latest.current = opts

  const pending = useRef<
    { id: number; x0: number; y0: number; payload: P; moved: boolean } | null
  >(null)
  // A completed drag is followed by a click event on the source element. That
  // click must not also count as a placement.
  const justDragged = useRef(false)

  const start = useCallback((event: ReactPointerEvent, payload: P) => {
    if (event.pointerType === 'touch') return
    if (event.button !== 0) return
    justDragged.current = false
    pending.current = {
      id: event.pointerId,
      x0: event.clientX,
      y0: event.clientY,
      payload,
      moved: false,
    }
  }, [])

  useEffect(() => {
    const move = (event: PointerEvent) => {
      const press = pending.current
      if (!press || event.pointerId !== press.id) return
      if (!press.moved) {
        const travelled = Math.hypot(event.clientX - press.x0, event.clientY - press.y0)
        if (travelled < THRESHOLD) return
        press.moved = true
      }
      event.preventDefault()
      setDrag({
        payload: press.payload,
        x: event.clientX,
        y: event.clientY,
        target: latest.current.resolve(event.clientX, event.clientY, press.payload),
      })
    }

    const end = (event: PointerEvent) => {
      const press = pending.current
      if (!press || event.pointerId !== press.id) return
      pending.current = null
      setDrag(null)
      if (!press.moved) return
      justDragged.current = true
      latest.current.onDrop(
        press.payload,
        latest.current.resolve(event.clientX, event.clientY, press.payload),
      )
    }

    const cancel = () => {
      pending.current = null
      setDrag(null)
    }

    window.addEventListener('pointermove', move, { passive: false })
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', cancel)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', end)
      window.removeEventListener('pointercancel', cancel)
    }
  }, [])

  /** True once, immediately after a drag — swallows the trailing click. */
  const consumeClick = useCallback(() => {
    const dragged = justDragged.current
    justDragged.current = false
    return dragged
  }, [])

  return { drag, start, consumeClick }
}
