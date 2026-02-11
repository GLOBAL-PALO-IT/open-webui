# Instructions to publish the Open WebUI Docker image to GitHub Container Registry

## 1. Build and tag the image:

1. Uncomment line 31:
    ```bash
    ENV NODE_OPTIONS="--max-old-space-size=4096"
    ``` 
    in the Dockerfile so that it doesn't fail when building the image.

2. Build the image with the following command:
    ```shell
    docker build --platform=linux/amd64 -t open-webui .
    ```
3. Tag the image for GitHub Container Registry:

    Get the latest commit hash from the repository:
    ```shell
    COMMIT_HASH=$(git rev-parse --short HEAD)

    Then tag the image using the following command:
    ```shell
    docker tag open-webui:latest ghcr.io/global-palo-it/open-webui:git-$COMMIT_HASH
    ```

## 2. Publish the image to GitHub Container Registry:

1. Create a Personal Access Token (PAT) (classic) with the `write:packages`, `read:packages`, and `delete:packages` scopes. in https://github.com/settings/tokens.

2. Login to GitHub Container Registry:

   Use the following command to log in:
   ```shell
   docker login ghcr.io -u YOUR_GITHUB_USERNAME
   ```
   
    Replace `YOUR_GITHUB_USERNAME` with your actual GitHub username. When prompted for a password, use the PAT you created in step 1.

3. Push the tagged image to GitHub Container Registry:

   Use the following command to push the image:
   ```shell
   docker push ghcr.io/global-palo-it/open-webui:git-$COMMIT_HASH
   ```