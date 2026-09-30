#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.spend_controls_stack import SpendControlsStack

app = cdk.App()
SpendControlsStack(
    app, "BedrockSpendControls",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Runtime-only spend controls for Amazon Bedrock",
)
app.synth()
