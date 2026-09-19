export const descriptions = {
  write: 'Write a UTF-8 candidate output file. Use the output.json or refusal.json path in instruction.md. '
    + 'The content is the complete file text. This does not validate or submit the file.',
  validate: 'Validate the candidate file using the full declared output model and business validators. '
    + 'Errors include field paths and explanations. Correct the file and validate again. This does not end the task.',
  submit: 'Submit output.json. The tool rereads and validates the file, saves an immutable accepted result, '
    + 'and ends the task only on success. A validation error allows further corrections.',
  refuse: 'Submit refusal.json containing a reason for an unanswerable input. The tool validates the '
    + 'file and ends the task on success. Use this only when the provided material cannot answer the task.',
}

export const alreadyAnswered = 'This task has already been submitted. Stop here.'
