FROM eclipse-temurin:21-jdk-jammy@sha256:d48e921b83506f58910295b35fd02978c7f4c57fef41bceee6a460d7d97bc251 AS build
WORKDIR /app
ADD https://github.com/hapifhir/org.hl7.fhir.core/releases/download/6.10.4/validator_cli.jar /app/validator.jar
RUN echo '1106b9d58f9e363e47bea7c4fc065841e5fc91fe9d062775c3bfdd212bd653cc  validator.jar' | sha256sum -c - && chmod 644 validator.jar
COPY Server.java .
RUN javac -proc:none -cp validator.jar Server.java

FROM eclipse-temurin:21-jre-jammy@sha256:54b382c9d790d3201004d4e3921f4eff7dc7d67e1ce21a2da8d8faaaf63c85fc
WORKDIR /app
COPY --from=build /app/validator.jar /app/*.class ./
COPY logback.xml /app/logback.xml
RUN mkdir /cache && chown 10001:10001 /cache
USER 10001:10001
ENV JAVA_TOOL_OPTIONS="-Xmx2g -XX:+ExitOnOutOfMemoryError -Duser.home=/work -Djava.io.tmpdir=/work -Duser.language=en -Duser.country=US -Dlogback.configurationFile=/app/logback.xml -Dsun.net.httpserver.maxReqTime=20 -Dsun.net.httpserver.maxConnections=32"
ENTRYPOINT ["java", "-cp", "/app:/app/validator.jar", "Server"]
