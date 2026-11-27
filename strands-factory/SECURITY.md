# Security Policy

Strands Factory is a reference architecture that you deploy and run in your own AWS account. Anything you give it, including tokens, permissions, and the data it can read, can be captured through prompt injection in the text it reads: issues, pull requests, comments, Slack messages, web pages, and files. Give the agent only what you are willing to have exposed. [Security model](docs/security.md) explains what the code protects and what you own.

## Support

Strands Factory is a devtool that the Strands team uses for its own work, not a supported product. There are no support guarantees and no versioned support policy. We fix the problems we find in `main`, and you get those fixes by pulling `main` and redeploying.

## Reporting Security Issues

Amazon Web Services (AWS) is dedicated to the responsible disclosure of security vulnerabilities.

We kindly ask that you **do not** open a public GitHub issue to report security concerns.

Instead, please submit the issue to the AWS Vulnerability Disclosure Program via [HackerOne](https://hackerone.com/aws_vdp) or send your report via [email](mailto:aws-security@amazon.com).

For more details, visit the [AWS Vulnerability Reporting Page](http://aws.amazon.com/security/vulnerability-reporting/).

Thank you in advance for collaborating with us to help protect our customers.
