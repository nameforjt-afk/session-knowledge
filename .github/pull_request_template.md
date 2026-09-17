## Summary

<!-- Describe the observable behavior changed by this pull request. -->

## Reproduction and approach

<!-- For a fix, explain how the regression test failed before the implementation. -->

## Verification

```text
python3 -m unittest discover -s tests -v
python3 -m compileall -q sessionmcp
```

## Checklist

- [ ] Linked issue included with `Closes #...`
- [ ] Tests use synthetic data and contain no real transcripts or credentials
- [ ] Complete test suite passes locally
- [ ] User-facing behavior is documented in English and Chinese where applicable
- [ ] Runtime remains free of third-party dependencies, or the linked issue approves an exception
