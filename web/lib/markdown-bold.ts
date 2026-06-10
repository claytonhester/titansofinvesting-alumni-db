export interface TextSegment {
  text: string;
  bold: boolean;
}

// The cohort narrative is Haiku-written and may emit **bold** around key figures
// (coverage count, MD rate, years-to-MD, founder count). We don't want a full
// markdown dependency for one inline feature, so this splits a string into
// ordered segments the renderer maps to <strong> or plain text. Unmatched or
// empty ** are treated as literal text, so a pre-bold (older) narrative renders
// unchanged as a single non-bold segment.
export function parseBoldSegments(input: string): TextSegment[] {
  const segments: TextSegment[] = [];
  const re = /\*\*(.+?)\*\*/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = re.exec(input)) !== null) {
    if (match.index > last) {
      segments.push({ text: input.slice(last, match.index), bold: false });
    }
    segments.push({ text: match[1], bold: true });
    last = match.index + match[0].length;
  }

  if (last < input.length) {
    segments.push({ text: input.slice(last), bold: false });
  }

  return segments;
}
