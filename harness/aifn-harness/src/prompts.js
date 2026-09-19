/**
 * Everything the model reads that this bundle decides on its own.
 *
 * The two tool descriptions and the prompt for one AI function are the caller's,
 * arriving in the call layer, because they are about that function. What is left
 * here is about the mechanism: the field a refusal carries, and the sentence a
 * second call is refused with. Keeping those in one file is what lets the
 * wording be reviewed without reading the registration around it.
 */

/** The one field a refusal carries, and what makes it useful to the caller. */
export const REASON_DESCRIPTION =
  'Why this input has no answer, in enough detail that somebody reading it alone '
  + 'can tell what was missing. This reaches the caller as the whole result, and '
  + 'nothing else from this session is recorded, so "cannot answer" on its own is '
  + 'indistinguishable from the tool having been called by mistake.'

/**
 * What a second answering call is refused with.
 *
 * A refusal has to teach rather than just deny: the model is mid-response, it
 * sees an error, and without a reason it retries the same call for the rest of
 * the turn. Naming the tool it just called is what makes the state readable.
 */
export const alreadyAnswered = tool =>
  `This call has already been answered; \`${tool}\` records nothing now. `
  + 'An AI function call answers once, and the answer it already carries is the '
  + 'one the caller reads. Stop here.'
