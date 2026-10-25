export const ALBION_ACT_RE = /<\|ACT\s*(?::\s*)?(\{[\s\S]*?\}\s*)\|>/i

/** Remove performance metadata from display text without changing the stored turn. */
export function stripAlbionAct(text: string): string {
  return text.replace(new RegExp(ALBION_ACT_RE.source, 'gi'), '').trim()
}
