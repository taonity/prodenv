FROM jenkins/jenkins:2.401.1-jdk11
USER root
RUN apt-get update && apt-get install -y lsb-release
RUN curl -fsSLo /usr/share/keyrings/docker-archive-keyring.asc \
  https://download.docker.com/linux/debian/gpg
RUN echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/usr/share/keyrings/docker-archive-keyring.asc] \
  https://download.docker.com/linux/debian \
  $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
RUN apt-get update && apt-get install -y docker-ce-cli docker-compose-plugin

#COPY domain.crt $JAVA_HOME/lib/security
#RUN \
#    cd $JAVA_HOME/lib/security \
#    && keytool -keystore cacerts -storepass changeit -noprompt -trustcacerts -importcert -alias domaincrt -file domain.crt

USER jenkins
RUN jenkins-plugin-cli --plugins "blueocean docker-workflow"