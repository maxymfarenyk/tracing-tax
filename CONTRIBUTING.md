# Contributing

## Commit Message Convention

> The commit message convention in this project is based on the
> [Git Commit Best Practices](https://cbea.ms/git-commit/)

- Title is no longer than 50 symbols.
- Each body line is no longer than 72 symbols.
- Title and body are separated by a blank line.

Title starts with any of the following verbs in imperative mood:

| Verb      | Purpose                                                                         | Example                                       |
|-----------|---------------------------------------------------------------------------------|-----------------------------------------------|
| Add       | The commit adds a new feature, package, dependency, test, etc.                  | Add DateTime scalar                           |
| Update    | The commit changes the value or the behavior of something                       | Update Jaeger image to 2.23.0                 |
| Move      | The commit moves or extracts file, piece of code or logic into another location | Move ariadne QueryType from schema to query   |
| Fix       | The commit resolves incorrect behaviors or values                               | Fix path to schema.graphql in spectaql config |
| Deprecate | The commit marks something as to be removed soon                                | Deprecate `namespace` query field             |
| Remove    | The commit removes a file, piece of code or logic                               | Remove deprecated `namespace` query field     |
