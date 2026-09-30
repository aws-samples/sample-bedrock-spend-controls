// Runtime configuration for the admin console.
//
// The values come from the stack outputs; the CDK deployment overwrites this
// file in the site bucket, so one build works for any deployment. The SPA reads
// window.QUOTA_ADMIN_CONFIG at load time. Do NOT put any secret here; every
// value is a public identifier.
window.QUOTA_ADMIN_CONFIG = {
  // Broker API URL (AWS_IAM Lambda Function URL).
  gatewayUrl: "",
  region: "us-east-1",
  // OIDC issuer URL. Sign-in endpoints are discovered from it.
  issuer: "",
  // Public (no-secret) OAuth client id registered for this console.
  clientId: "",
  // Cognito Identity Pool that exchanges the ID token for AWS credentials.
  identityPoolId: "",
  // Optional: space-separated OAuth scopes (default "openid email profile").
  // scopes: "openid email profile",
};
