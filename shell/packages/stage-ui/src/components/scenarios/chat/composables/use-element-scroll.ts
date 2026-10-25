import type { MaybeComputedElementRef } from '@vueuse/core'

import { useElementBounding, useEventListener, useResizeObserver, useWindowSize } from '@vueuse/core'
import { clamp } from 'es-toolkit'
import { computed, onBeforeUnmount, shallowRef, toValue, watchEffect } from 'vue'

export function useElementScroll(
  element: MaybeComputedElementRef,
  scrollTarget: MaybeComputedElementRef<HTMLElement | null | undefined>,
) {
  const scrollOffset = shallowRef(0)
  const effectiveScrollTarget = shallowRef<HTMLElement | null>(null)
  const stopScrollListener = shallowRef<(() => void) | null>(null)

  const { height: windowHeight } = useWindowSize()
  const {
    top: elementTop,
    height: elementHeight,
    update: updateElementBounds,
  } = useElementBounding(element, {
    immediate: true,
    reset: false,
    windowResize: true,
    windowScroll: true,
  })

  const resolvedScrollTarget = computed(() => toValue(scrollTarget) ?? null)

  // NOTICE:
  // The scroll viewport is read on demand instead of through useElementBounding.
  // Chat messages call this composable while Virtua mounts them inside its own
  // ResizeObserver callback. A per-message observer on the shallower viewport is
  // created too late in that delivery, so Chromium reports
  // "ResizeObserver loop completed with undelivered notifications" for each mount.
  // Scroll, window scroll/resize, and the measured element's resize call this update.
  // A viewport that only changes height refreshes on its next scroll.
  // Source: https://www.w3.org/TR/resize-observer/, node_modules/@vueuse/core useElementBounding.
  // Remove this when the viewport bounds come from one observer owned by the scroll container.
  const scrollViewportTop = shallowRef(0)
  const scrollViewportBottom = shallowRef(0)
  const scrollViewportHeight = shallowRef(0)

  function updateScrollViewportBounds() {
    const target = resolvedScrollTarget.value
    if (!target) {
      scrollViewportTop.value = 0
      scrollViewportBottom.value = 0
      scrollViewportHeight.value = 0
      return
    }

    const rect = target.getBoundingClientRect()
    scrollViewportTop.value = rect.top
    scrollViewportBottom.value = rect.bottom
    scrollViewportHeight.value = target.clientHeight
  }

  function syncEffectiveScrollTarget() {
    const target = resolvedScrollTarget.value
    effectiveScrollTarget.value = target && target.scrollHeight > target.clientHeight
      ? target
      : null
  }

  function bindScrollTarget(target: HTMLElement | null | undefined) {
    stopScrollListener.value?.()
    stopScrollListener.value = null

    scrollOffset.value = target?.scrollTop ?? 0
    updateScrollViewportBounds()
    if (!target)
      return

    stopScrollListener.value = useEventListener(target, 'scroll', () => {
      scrollOffset.value = target.scrollTop
      updateElementBounds()
      updateScrollViewportBounds()
    }, { passive: true })
  }

  const viewportTop = computed(() => effectiveScrollTarget.value ? scrollViewportTop.value : 0)
  const viewportBottom = computed(() => effectiveScrollTarget.value ? scrollViewportBottom.value : windowHeight.value)
  const viewportHeight = computed(() => effectiveScrollTarget.value ? scrollViewportHeight.value : windowHeight.value)
  const elementTopInScrollContent = computed(() => {
    if (!effectiveScrollTarget.value)
      return elementTop.value

    return elementTop.value - scrollViewportTop.value + scrollOffset.value
  })
  const visibleStart = computed(() => {
    if (!effectiveScrollTarget.value)
      return Math.max(viewportTop.value - elementTop.value, 0)

    return clamp(scrollOffset.value - elementTopInScrollContent.value, 0, elementHeight.value)
  })
  const visibleEnd = computed(() => {
    if (!effectiveScrollTarget.value)
      return Math.min(viewportBottom.value - elementTop.value, elementHeight.value)

    return clamp(scrollOffset.value + viewportHeight.value - elementTopInScrollContent.value, 0, elementHeight.value)
  })
  const innerTop = computed(() => elementTop.value + visibleStart.value)
  const innerBottom = computed(() => elementTop.value + visibleEnd.value)
  const innerHeight = computed(() => Math.max(0, visibleEnd.value - visibleStart.value))
  const hasMeasuredElement = computed(() => elementHeight.value > 0)
  const isVisible = computed(() => hasMeasuredElement.value && innerHeight.value > 0)

  watchEffect(() => {
    syncEffectiveScrollTarget()
    bindScrollTarget(effectiveScrollTarget.value)
    updateElementBounds()
    updateScrollViewportBounds()
  })

  useResizeObserver(element, () => {
    syncEffectiveScrollTarget()
    updateElementBounds()
    updateScrollViewportBounds()
  })

  // Captures scrolling ancestors, which move the viewport without resizing it.
  useEventListener(window, 'scroll', updateScrollViewportBounds, { capture: true, passive: true })

  useEventListener(window, 'resize', () => {
    syncEffectiveScrollTarget()
    bindScrollTarget(effectiveScrollTarget.value)
    updateElementBounds()
    updateScrollViewportBounds()
  }, { passive: true })

  onBeforeUnmount(() => {
    stopScrollListener.value?.()
  })

  return {
    scrollOffset,

    viewportTop,
    viewportBottom,
    viewportHeight,

    elementTop,
    elementHeight,
    elementTopInScrollContent,

    visibleStart,
    visibleEnd,

    innerTop,
    innerBottom,
    innerHeight,

    hasMeasuredElement,
    isVisible,
    scrollTarget: effectiveScrollTarget,

    updateElementBounds,
    updateScrollViewportBounds,
  }
}
