'use strict';

// Basic Auth credentials for Strands Evals Dashboard
// These placeholders are replaced at CDK deploy time with values from Secrets Manager
const USERNAME = '__BASIC_AUTH_USERNAME__';
const PASSWORD = '__BASIC_AUTH_PASSWORD__';

// Pre-compute the expected Authorization header value
const EXPECTED_AUTH = 'Basic ' + Buffer.from(`${USERNAME}:${PASSWORD}`).toString('base64');

exports.handler = async (event) => {
    const request = event.Records[0].cf.request;
    const headers = request.headers;

    const authHeader = headers.authorization?.[0]?.value;

    if (authHeader === EXPECTED_AUTH) {
        // Credentials match - allow request to proceed
        return request;
    }

    // Return 401 Unauthorized with WWW-Authenticate header to trigger browser login prompt
    return {
        status: '401',
        statusDescription: 'Unauthorized',
        headers: {
            'www-authenticate': [{
                key: 'WWW-Authenticate',
                value: 'Basic realm="Strands Evals Dashboard"'
            }],
            'content-type': [{
                key: 'Content-Type',
                value: 'text/html'
            }]
        },
        body: '<html><body><h1>401 Unauthorized</h1><p>Authentication required.</p></body></html>'
    };
};
